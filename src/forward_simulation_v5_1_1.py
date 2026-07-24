#!/usr/bin/env python3
"""SQLite-backed forward simulation v5.1.1 RC1.

This release-candidate module is intentionally separate from v5.1. It fixes
the RC1 blocking issues without overwriting the v5.1 files or ledgers.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import platform
import shutil
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


VERSION = "forward_simulation_v5.1.1-rc1"
SCHEMA_VERSION = "forward_v5_1_1_schema_001"
FORMAL = "formal"
DEMO = "demo"
EPS = 1e-9
FROZEN_TEST_TIME = "2026-07-21T00:00:00+00:00"

STRATEGIES: dict[str, dict[str, Any]] = {
    "hold_to_settlement": {"multiple": None, "fraction": 0.0, "stage": "hold"},
    "tp_2x_sell_50pct": {"multiple": 2.0, "fraction": 0.50, "stage": "tp_2x_once"},
    "tp_2x_sell_75pct": {"multiple": 2.0, "fraction": 0.75, "stage": "tp_2x_once"},
    "tp_5x_sell_25pct": {"multiple": 5.0, "fraction": 0.25, "stage": "tp_5x_once"},
}
STRATEGY_IDS = list(STRATEGIES)

USER_SIGNAL_FIELDS = [
    "signal_id",
    "created_at_utc",
    "city",
    "weather_date_local",
    "weather_metric",
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
    "config_sha256": "config/forward_simulation_v5_1_1.yaml",
    "core_code_sha256": "src/forward_simulation_v5_1_1.py",
    "reporting_code_sha256": "src/forward_reporting_v5_1_1.py",
    "schema_sha256": "schemas/forward_simulation_v5_1_1.sql",
    "preregistration_sha256": "reports/FORWARD_SIMULATION_V5_1_PREREGISTRATION.md",
}
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def now_iso(now: datetime | None = None) -> str:
    return (now or utcnow()).astimezone(timezone.utc).isoformat()


def parse_utc(value: str, require_utc: bool = False) -> datetime:
    if not value:
        raise ValueError("missing UTC timestamp")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("timestamp must include UTC timezone")
    if require_utc and dt.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be UTC")
    dt = dt.astimezone(timezone.utc)
    return dt


def fnum(value: Any, default: float = 0.0) -> float:
    try:
        if value in ("", None):
            return default
        x = float(value)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def fee(gross: float, bps: float) -> float:
    return round(gross * bps / 10000.0, 8)


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def normalize_city(city: str) -> str:
    return " ".join((city or "").strip().lower().split())


def normalize_metric(metric: str) -> str:
    raw = " ".join((metric or "").strip().lower().split())
    aliases = {
        "high": "high",
        "highest": "high",
        "max": "high",
        "highest temperature": "high",
        "low": "low",
        "lowest": "low",
        "min": "low",
        "lowest temperature": "low",
    }
    return aliases.get(raw, raw)


def make_event_key(city: str, weather_date_local: str, weather_metric: str) -> str:
    return "|".join([normalize_city(city), weather_date_local.strip(), normalize_metric(weather_metric)])


def parse_scalar(value: str) -> Any:
    if value in {"null", "None", "~"}:
        return None
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        return value.strip("\"'")


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
        key, _, value = line.strip().partition(":")
        while indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if value.strip() == "":
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = parse_scalar(value.strip())
    return out


def data_dir(root: Path, mode: str, config: dict[str, Any] | None = None) -> Path:
    if config:
        rel = config.get("paths", {}).get(f"{mode}_data_dir")
        if rel:
            return root / str(rel)
    return root / "data/forward_v5_1_1" / mode


def db_path(root: Path, mode: str, config: dict[str, Any] | None = None) -> Path:
    return data_dir(root, mode, config) / "ledger.sqlite3"


def safe_audit_path(root: Path, mode: str, config: dict[str, Any] | None = None) -> Path:
    return data_dir(root, mode, config) / "safe_audit.jsonl"


def schema_path(root: Path, config: dict[str, Any] | None = None) -> Path:
    rel = (config or {}).get("paths", {}).get("schema_file", "schemas/forward_simulation_v5_1_1.sql")
    p = root / str(rel)
    return p if p.exists() else PROJECT_ROOT / str(rel)


def prereg_path(root: Path, config: dict[str, Any] | None = None) -> Path:
    rel = (config or {}).get("paths", {}).get("preregistration_file", "reports/FORWARD_SIMULATION_V5_1_PREREGISTRATION.md")
    p = root / str(rel)
    return p if p.exists() else PROJECT_ROOT / str(rel)


def append_safe_audit(root: Path, mode: str, event_type: str, payload: dict[str, Any], config: dict[str, Any] | None = None, now: datetime | None = None) -> str:
    path = safe_audit_path(root, mode, config)
    path.parent.mkdir(parents=True, exist_ok=True)
    audit_id = "aud_" + sha256_text(stable_json({"t": now_iso(now), "event": event_type, "payload": payload}))[:24]
    row = {"audit_id": audit_id, "created_at_utc": now_iso(now), "event_type": event_type, "payload": payload}
    with path.open("a", encoding="utf-8") as f:
        f.write(stable_json(row) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return audit_id


def read_safe_audit(root: Path, mode: str, config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    p = safe_audit_path(root, mode, config)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def connect(db: Path) -> sqlite3.Connection:
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


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
    safe_audit_path(root, mode, config).touch(exist_ok=True)
    return db


def set_state(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute("INSERT OR REPLACE INTO state(key,value) VALUES(?,?)", (key, str(value)))


def get_state(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def state_dict(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["key"]: r["value"] for r in conn.execute("SELECT key,value FROM state")}


def current_hashes(root: Path, config_path: Path) -> dict[str, str]:
    if not config_path.exists():
        raise FileNotFoundError(f"configuration file not found: {config_path}")
    config = load_config(config_path)
    paths = {
        "config_sha256": config_path,
        "core_code_sha256": root / "src/forward_simulation_v5_1_1.py",
        "reporting_code_sha256": root / "src/forward_reporting_v5_1_1.py",
        "schema_sha256": schema_path(root, config),
        "preregistration_sha256": prereg_path(root, config),
    }
    return {k: file_sha256(v) for k, v in paths.items()}


def assert_formal_hashes(root: Path, mode: str, config_path: Path, conn: sqlite3.Connection | None = None) -> None:
    if mode != FORMAL:
        return
    own_conn = None
    config: dict[str, Any] | None = None
    try:
        config = load_config(config_path)
        own_conn = conn or connect(db_path(root, mode, config))
        started = get_state(own_conn, "formal_started_at_utc", "")
        if not started:
            raise RuntimeError("formal sample is not started")
        current = current_hashes(root, config_path)
        drift = {k: {"expected": get_state(own_conn, k, ""), "current": v} for k, v in current.items() if get_state(own_conn, k, "") != v}
        if drift:
            append_safe_audit(root, mode, "hash_freeze_reject", {"drift": drift}, config)
            raise RuntimeError("formal hash freeze mismatch; refusing to write business ledger")
    finally:
        if own_conn is not None and conn is None:
            own_conn.close()


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
            for k, v in hashes.items():
                set_state(conn, k, v)
        audit_id = append_safe_audit(root, FORMAL, "formal_started_v5_1_1", {"hashes": hashes, "started_at_utc": now_iso(now)}, config, now)
        return {"status": "started", "audit_id": audit_id, "formal_started_at_utc": now_iso(now)}
    finally:
        conn.close()


def id_for(prefix: str, payload: dict[str, Any]) -> str:
    return prefix + "_" + sha256_text(stable_json(payload))[:24]


def normalize_level(level: dict[str, Any]) -> dict[str, float] | None:
    price = fnum(level.get("price"), math.nan)
    size = fnum(level.get("size"), math.nan)
    if math.isfinite(price) and math.isfinite(size) and price > 0 and size > 0:
        return {"price": price, "size": size}
    return None


def normalize_book(raw: dict[str, Any]) -> dict[str, list[dict[str, float]]]:
    bids = [x for x in (normalize_level(v) for v in raw.get("bids", [])) if x]
    asks = [x for x in (normalize_level(v) for v in raw.get("asks", [])) if x]
    return {"bids": sorted(bids, key=lambda x: x["price"], reverse=True), "asks": sorted(asks, key=lambda x: x["price"])}


def best_bid_ask(book: dict[str, list[dict[str, float]]]) -> tuple[float | None, float | None, float | None]:
    bid = book["bids"][0]["price"] if book["bids"] else None
    ask = book["asks"][0]["price"] if book["asks"] else None
    spread = ask - bid if bid is not None and ask is not None else None
    return bid, ask, spread


def snapshot_id(token_id: str, purpose: str, raw: dict[str, Any]) -> str:
    server_ts = raw.get("timestamp") or raw.get("serverTime") or raw.get("updated_at") or ""
    return id_for("ob", {"token_id": token_id, "purpose": purpose, "server_timestamp": server_ts, "book": normalize_book(raw)})


def record_snapshot(conn: sqlite3.Connection, token_id: str, purpose: str, raw: dict[str, Any], mode: str, source: str, now: datetime | None) -> str:
    sid = snapshot_id(token_id, purpose, raw)
    exists = conn.execute("SELECT 1 FROM orderbook_snapshots WHERE snapshot_id=?", (sid,)).fetchone()
    if not exists:
        conn.execute(
            "INSERT INTO orderbook_snapshots(snapshot_id,captured_at_utc,token_id,purpose,raw_orderbook_json,source,mode) VALUES(?,?,?,?,?,?,?)",
            (sid, now_iso(now), token_id, purpose, stable_json(raw), source, mode),
        )
    return sid


def consume_buy_depth(levels: list[dict[str, float]], intended_usd: float, max_price: float) -> dict[str, Any]:
    remaining = intended_usd
    shares = gross = 0.0
    used: list[dict[str, float]] = []
    for level in levels:
        if remaining <= EPS or level["price"] > max_price + EPS:
            break
        qty = min(level["size"], remaining / level["price"])
        if qty <= EPS:
            continue
        usd = qty * level["price"]
        level["size"] -= qty
        shares += qty
        gross += usd
        remaining -= usd
        used.append({"price": level["price"], "shares": qty, "usd": usd})
    return {"shares": shares, "gross": gross, "remaining_usd": max(remaining, 0.0), "vwap": gross / shares if shares > EPS else math.nan, "levels": used}


def probe_sell_depth(levels: list[dict[str, float]], shares_to_sell: float) -> dict[str, Any]:
    remaining = shares_to_sell
    shares = gross = 0.0
    used: list[dict[str, float]] = []
    for level in levels:
        if remaining <= EPS:
            break
        qty = min(level["size"], remaining)
        if qty <= EPS:
            continue
        usd = qty * level["price"]
        shares += qty
        gross += usd
        remaining -= qty
        used.append({"price": level["price"], "shares": qty, "usd": usd})
    return {"shares": shares, "gross": gross, "remaining_shares": max(remaining, 0.0), "vwap": gross / shares if shares > EPS else math.nan, "levels": used}


def consume_sell_depth(levels: list[dict[str, float]], shares_to_sell: float) -> dict[str, Any]:
    result = probe_sell_depth(levels, shares_to_sell)
    remaining = shares_to_sell
    for level in levels:
        if remaining <= EPS:
            break
        qty = min(level["size"], remaining)
        level["size"] -= qty
        remaining -= qty
    return result


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def signal_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {k: str(row.get(k, "")) for k in USER_SIGNAL_FIELDS}


def signal_hash(row: dict[str, Any]) -> str:
    return sha256_text(stable_json(signal_payload(row)))


def validate_signal(row: dict[str, Any], mode: str, conn: sqlite3.Connection, config: dict[str, Any], now: datetime) -> tuple[dict[str, Any], str]:
    if row.get("registered_at_utc"):
        raise ValueError("user-supplied registered_at_utc is forbidden")
    created = parse_utc(str(row.get("created_at_utc", "")), require_utc=True)
    if mode == FORMAL:
        started = get_state(conn, "formal_started_at_utc", "")
        if not started:
            raise ValueError("formal sample is not started")
        if created < parse_utc(started) - timedelta(microseconds=1):
            raise ValueError("signal before formal start")
        delay = (now - created).total_seconds()
        max_delay = fnum(config["sample_rules"].get("max_signal_registration_delay_seconds", 300))
        future = fnum(config["sample_rules"].get("allowed_future_skew_seconds", 30))
        if delay > max_delay:
            raise ValueError("signal registration delay exceeded")
        if delay < -future:
            raise ValueError("signal timestamp is too far in the future")
    if str(row.get("side", "")).upper() != "BUY":
        raise ValueError("only BUY entry signals are supported")
    if not row.get("token_id") or not row.get("condition_id"):
        raise ValueError("token_id and condition_id are required")
    if row.get("market_token_id") and row.get("market_token_id") != row.get("token_id"):
        raise ValueError("market token metadata does not match signal token")
    if row.get("market_condition_id") and row.get("market_condition_id") != row.get("condition_id"):
        raise ValueError("market condition metadata does not match signal condition")
    if row.get("market_weather_date_local") and row.get("market_weather_date_local") != row.get("weather_date_local"):
        raise ValueError("market date metadata does not match signal date")
    intended = fnum(row.get("intended_usd"), math.nan)
    max_price = fnum(row.get("max_entry_price"), math.nan)
    if not math.isfinite(intended) or intended <= 0 or not math.isfinite(max_price) or max_price <= 0:
        raise ValueError("intended_usd and max_entry_price must be positive")
    valid_minutes = fnum(config["entry"].get("entry_valid_minutes", 10))
    metric = normalize_metric(str(row.get("weather_metric", "high")))
    event_key = make_event_key(str(row.get("city", "")), str(row.get("weather_date_local", "")), metric)
    payload = {
        **signal_payload(row),
        "created_at_utc": created.isoformat(),
        "registered_at_utc": now.isoformat(),
        "city_normalized": normalize_city(str(row.get("city", ""))),
        "weather_metric": metric,
        "event_key": event_key,
        "entry_deadline_utc": (created + timedelta(minutes=valid_minutes)).isoformat(),
        "intended_usd": intended,
        "max_entry_price": max_price,
        "forecast_probability": fnum(row.get("forecast_probability"), math.nan),
        "market_probability_at_signal": fnum(row.get("market_probability_at_signal"), math.nan),
        "mode": mode,
    }
    return payload, signal_hash(row)


def register_signals(root: Path, mode: str, config_path: Path, signals_file: Path, now: datetime | None = None) -> list[dict[str, Any]]:
    config = load_config(config_path)
    db = init_ledger(root, mode, config_path)
    now = (now or utcnow()).astimezone(timezone.utc)
    conn = connect(db)
    accepted: list[dict[str, Any]] = []
    try:
        assert_formal_hashes(root, mode, config_path, conn)
        rows = read_csv_rows(signals_file)
        with conn:
            for row in rows:
                sid = row.get("signal_id", "")
                try:
                    payload, sig_hash = validate_signal(row, mode, conn, config, now)
                    existing = conn.execute("SELECT * FROM signals WHERE signal_id=? ORDER BY row_id", (sid,)).fetchall()
                    if existing:
                        if any(r["signal_hash"] != sig_hash for r in existing):
                            append_safe_audit(root, mode, "signal_duplicate_conflict_rejected", {"signal_id": sid}, config, now)
                            continue
                        accepted.append(dict(existing[-1]))
                        continue
                    audit_id = append_safe_audit(root, mode, "signal_registered", {"signal_id": sid, "signal_hash": sig_hash, "event_key": payload["event_key"]}, config, now)
                    conn.execute(
                        """
                        INSERT INTO signals(signal_id,signal_hash,registration_audit_id,created_at_utc,registered_at_utc,city,city_normalized,weather_date_local,weather_metric,event_key,market_slug,condition_id,token_id,outcome,side,forecast_temperature,forecast_probability,market_probability_at_signal,intended_usd,max_entry_price,entry_deadline_utc,source,notes,mode)
                        VALUES(:signal_id,:signal_hash,:registration_audit_id,:created_at_utc,:registered_at_utc,:city,:city_normalized,:weather_date_local,:weather_metric,:event_key,:market_slug,:condition_id,:token_id,:outcome,:side,:forecast_temperature,:forecast_probability,:market_probability_at_signal,:intended_usd,:max_entry_price,:entry_deadline_utc,:source,:notes,:mode)
                        """,
                        {**payload, "signal_hash": sig_hash, "registration_audit_id": audit_id},
                    )
                    conn.execute(
                        "INSERT INTO entry_order_state(signal_id,token_id,updated_at_utc,intended_usd,filled_entry_usd,remaining_entry_usd,filled_entry_shares,entry_status,max_entry_price,entry_deadline_utc,last_entry_attempt_at,last_attempt_reason,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (sid, payload["token_id"], now.isoformat(), payload["intended_usd"], 0.0, payload["intended_usd"], 0.0, "pending", payload["max_entry_price"], payload["entry_deadline_utc"], "", "registered", mode),
                    )
                    accepted.append(payload)
                except Exception as exc:
                    append_safe_audit(root, mode, "signal_rejected", {"signal_id": sid, "reason": str(exc)}, config, now)
        return accepted
    finally:
        conn.close()


def latest_entry_state(conn: sqlite3.Connection, signal_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM entry_order_state WHERE signal_id=? ORDER BY row_id DESC LIMIT 1", (signal_id,)).fetchone()


def get_signal(conn: sqlite3.Connection, signal_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM signals WHERE signal_id=? ORDER BY row_id DESC LIMIT 1", (signal_id,)).fetchone()
    if row is None:
        raise KeyError(f"unknown signal_id: {signal_id}")
    return row


def list_active_entry_signals(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT s.* FROM signals s
        JOIN (
          SELECT signal_id, MAX(row_id) AS max_row_id FROM entry_order_state GROUP BY signal_id
        ) latest ON latest.signal_id=s.signal_id
        JOIN entry_order_state st ON st.row_id=latest.max_row_id
        WHERE st.entry_status IN ('pending','partial')
        ORDER BY s.created_at_utc, s.signal_id
        """
    ).fetchall()


def failpoint(name: str, hooks: dict[str, Any] | None) -> None:
    if hooks and hooks.get(name):
        raise RuntimeError(f"failpoint:{name}")


def process_entry_batch(
    root: Path,
    mode: str,
    config_path: Path,
    signal_ids: list[str],
    books_by_token: dict[str, dict[str, Any]],
    source: str = "fixture",
    now: datetime | None = None,
    failpoints: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    config = load_config(config_path)
    db = init_ledger(root, mode, config_path)
    now = (now or utcnow()).astimezone(timezone.utc)
    conn = connect(db)
    results: list[dict[str, Any]] = []
    try:
        assert_formal_hashes(root, mode, config_path, conn)
        with conn:
            signals = [get_signal(conn, sid) for sid in signal_ids]
            signals = sorted(signals, key=lambda r: (r["created_at_utc"], r["signal_id"]))
            for token_id in sorted({s["token_id"] for s in signals}):
                token_signals = [s for s in signals if s["token_id"] == token_id]
                raw = books_by_token[token_id]
                book = normalize_book(raw)
                sid = record_snapshot(conn, token_id, "entry", raw, mode, source, now)
                bid, ask, spread = best_bid_ask(book)
                ask_levels = [dict(level) for level in book["asks"]]
                for sig in token_signals:
                    state = latest_entry_state(conn, sig["signal_id"])
                    if not state or state["entry_status"] in {"filled", "expired", "cancelled"}:
                        results.append({"signal_id": sig["signal_id"], "status": "skipped"})
                        continue
                    if now > parse_utc(sig["entry_deadline_utc"]):
                        conn.execute(
                            "INSERT INTO entry_order_state(signal_id,token_id,updated_at_utc,intended_usd,filled_entry_usd,remaining_entry_usd,filled_entry_shares,entry_status,max_entry_price,entry_deadline_utc,last_entry_attempt_at,last_attempt_reason,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (sig["signal_id"], sig["token_id"], now.isoformat(), sig["intended_usd"], state["filled_entry_usd"], state["remaining_entry_usd"], state["filled_entry_shares"], "expired", sig["max_entry_price"], sig["entry_deadline_utc"], now.isoformat(), "entry_deadline_reached", mode),
                        )
                        results.append({"signal_id": sig["signal_id"], "status": "expired"})
                        continue
                    duplicate = conn.execute("SELECT 1 FROM entry_fills WHERE signal_id=? AND snapshot_id=?", (sig["signal_id"], sid)).fetchone()
                    if duplicate:
                        results.append({"signal_id": sig["signal_id"], "status": "skipped_duplicate_snapshot"})
                        continue
                    buy = consume_buy_depth(ask_levels, state["remaining_entry_usd"], sig["max_entry_price"])
                    if buy["shares"] <= EPS:
                        conn.execute(
                            "INSERT INTO entry_order_state(signal_id,token_id,updated_at_utc,intended_usd,filled_entry_usd,remaining_entry_usd,filled_entry_shares,entry_status,max_entry_price,entry_deadline_utc,last_entry_attempt_at,last_attempt_reason,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (sig["signal_id"], sig["token_id"], now.isoformat(), sig["intended_usd"], state["filled_entry_usd"], state["remaining_entry_usd"], state["filled_entry_shares"], state["entry_status"], sig["max_entry_price"], sig["entry_deadline_utc"], now.isoformat(), "ask_above_max_or_no_depth", mode),
                        )
                        results.append({"signal_id": sig["signal_id"], "status": "not_filled", "snapshot_id": sid})
                        continue
                    entry_fee = fee(buy["gross"], fnum(config["fees"].get("entry_fee_bps", 0)))
                    fill_id = id_for("entry", {"signal_id": sig["signal_id"], "snapshot_id": sid, "previous": state["filled_entry_usd"], "gross": buy["gross"]})
                    new_filled_usd = state["filled_entry_usd"] + buy["gross"]
                    new_filled_shares = state["filled_entry_shares"] + buy["shares"]
                    status = "filled" if new_filled_usd >= sig["intended_usd"] - 1e-6 else "partial"
                    conn.execute(
                        "INSERT INTO entry_fills(entry_fill_id,signal_id,event_key,token_id,snapshot_id,filled_at_utc,gross_entry_cost,entry_fee,total_entry_cost,filled_shares,entry_vwap,best_bid,best_ask,spread,complete_fill,unfilled_usd_after_fill,depth_levels_json,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (fill_id, sig["signal_id"], sig["event_key"], sig["token_id"], sid, now.isoformat(), buy["gross"], entry_fee, buy["gross"] + entry_fee, buy["shares"], buy["vwap"], bid, ask, spread, int(status == "filled"), max(sig["intended_usd"] - new_filled_usd, 0.0), stable_json(buy["levels"]), mode),
                    )
                    failpoint("after_entry_fill", failpoints)
                    for strategy_id in STRATEGY_IDS:
                        conn.execute(
                            "INSERT INTO strategy_lots(lot_id,strategy_id,signal_id,event_key,token_id,entry_fill_id,created_at_utc,entry_shares,gross_entry_cost,entry_fee,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (id_for("lot", {"strategy_id": strategy_id, "entry_fill_id": fill_id}), strategy_id, sig["signal_id"], sig["event_key"], sig["token_id"], fill_id, now.isoformat(), buy["shares"], buy["gross"], entry_fee, mode),
                        )
                    conn.execute(
                        "INSERT INTO entry_order_state(signal_id,token_id,updated_at_utc,intended_usd,filled_entry_usd,remaining_entry_usd,filled_entry_shares,entry_status,max_entry_price,entry_deadline_utc,last_entry_attempt_at,last_attempt_reason,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (sig["signal_id"], sig["token_id"], now.isoformat(), sig["intended_usd"], new_filled_usd, max(sig["intended_usd"] - new_filled_usd, 0.0), new_filled_shares, status, sig["max_entry_price"], sig["entry_deadline_utc"], now.isoformat(), status, mode),
                    )
                    results.append({"signal_id": sig["signal_id"], "status": status, "snapshot_id": sid, "filled_shares": buy["shares"], "filled_usd": buy["gross"]})
        return results
    finally:
        conn.close()


def process_entry(root: Path, mode: str, config_path: Path, signal_id: str, raw_book: dict[str, Any], **kwargs) -> dict[str, Any]:
    config = load_config(config_path)
    conn = connect(db_path(root, mode, config))
    try:
        token_id = get_signal(conn, signal_id)["token_id"]
    finally:
        conn.close()
    return process_entry_batch(root, mode, config_path, [signal_id], {token_id: raw_book}, **kwargs)[0]


def lot_open_rows(conn: sqlite3.Connection, signal_id: str, strategy_id: str) -> list[dict[str, Any]]:
    lots = conn.execute("SELECT * FROM strategy_lots WHERE signal_id=? AND strategy_id=? ORDER BY created_at_utc,row_id", (signal_id, strategy_id)).fetchall()
    out: list[dict[str, Any]] = []
    for lot in lots:
        sold = conn.execute("SELECT COALESCE(SUM(allocated_shares),0) AS v FROM exit_fill_allocations WHERE lot_id=? AND strategy_id=?", (lot["lot_id"], strategy_id)).fetchone()["v"]
        settled = conn.execute("SELECT COALESCE(SUM(settled_shares),0) AS v FROM settlement_allocations WHERE lot_id=? AND strategy_id=?", (lot["lot_id"], strategy_id)).fetchone()["v"]
        open_shares = lot["entry_shares"] - sold - settled
        if open_shares > EPS:
            unit_cost = (lot["gross_entry_cost"] + lot["entry_fee"]) / lot["entry_shares"]
            out.append({**dict(lot), "open_shares": open_shares, "unit_cost": unit_cost})
    return out


def signal_position_conn(conn: sqlite3.Connection, signal_id: str, strategy_id: str) -> dict[str, float]:
    lots = lot_open_rows(conn, signal_id, strategy_id)
    shares = sum(l["open_shares"] for l in lots)
    cost = sum(l["open_shares"] * l["unit_cost"] for l in lots)
    return {"shares": shares, "cost": cost, "avg_cost": cost / shares if shares > EPS else math.nan}


def signal_position(root: Path, mode: str, config_path: Path, signal_id: str, strategy_id: str) -> dict[str, float]:
    config = load_config(config_path)
    conn = connect(db_path(root, mode, config))
    try:
        return signal_position_conn(conn, signal_id, strategy_id)
    finally:
        conn.close()


def latest_trigger(conn: sqlite3.Connection, signal_id: str, strategy_id: str, stage: str) -> sqlite3.Row | None:
    tid = id_for("trig", {"signal_id": signal_id, "strategy_id": strategy_id, "stage": stage})
    return conn.execute("SELECT * FROM strategy_triggers WHERE trigger_id=? ORDER BY row_id DESC LIMIT 1", (tid,)).fetchone()


def create_trigger(conn: sqlite3.Connection, sig: sqlite3.Row, strategy_id: str, position: dict[str, float], mode: str, now: datetime) -> sqlite3.Row:
    strategy = STRATEGIES[strategy_id]
    tid = id_for("trig", {"signal_id": sig["signal_id"], "strategy_id": strategy_id, "stage": strategy["stage"]})
    target = position["shares"] * float(strategy["fraction"])
    threshold = position["avg_cost"] * float(strategy["multiple"])
    conn.execute(
        "INSERT INTO strategy_triggers(trigger_id,signal_id,strategy_id,trigger_stage_id,event_key,token_id,trigger_created_at,trigger_target_shares,trigger_filled_shares,trigger_remaining_shares,trigger_status,trigger_completed_at,rolling_avg_cost_at_trigger,threshold_price,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (tid, sig["signal_id"], strategy_id, strategy["stage"], sig["event_key"], sig["token_id"], now.isoformat(), target, 0.0, target, "open", "", position["avg_cost"], threshold, mode),
    )
    return latest_trigger(conn, sig["signal_id"], strategy_id, strategy["stage"])  # type: ignore[return-value]


def update_trigger(conn: sqlite3.Connection, trigger: sqlite3.Row, add_filled: float, mode: str, now: datetime) -> None:
    filled = trigger["trigger_filled_shares"] + add_filled
    remaining = max(trigger["trigger_target_shares"] - filled, 0.0)
    status = "completed" if remaining <= 1e-6 else "open"
    conn.execute(
        "INSERT INTO strategy_triggers(trigger_id,signal_id,strategy_id,trigger_stage_id,event_key,token_id,trigger_created_at,trigger_target_shares,trigger_filled_shares,trigger_remaining_shares,trigger_status,trigger_completed_at,rolling_avg_cost_at_trigger,threshold_price,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (trigger["trigger_id"], trigger["signal_id"], trigger["strategy_id"], trigger["trigger_stage_id"], trigger["event_key"], trigger["token_id"], trigger["trigger_created_at"], trigger["trigger_target_shares"], filled, remaining, status, now.isoformat() if status == "completed" else "", trigger["rolling_avg_cost_at_trigger"], trigger["threshold_price"], mode),
    )


def allocate_fifo(conn: sqlite3.Connection, signal_id: str, strategy_id: str, shares: float, gross: float, exit_fee: float) -> list[dict[str, Any]]:
    remaining = shares
    allocs: list[dict[str, Any]] = []
    for lot in lot_open_rows(conn, signal_id, strategy_id):
        if remaining <= EPS:
            break
        qty = min(lot["open_shares"], remaining)
        ratio = qty / shares if shares > EPS else 0
        allocs.append({"lot": lot, "shares": qty, "gross": gross * ratio, "fee": exit_fee * ratio})
        remaining -= qty
    if remaining > 1e-6:
        raise RuntimeError("FIFO allocation could not cover sold shares")
    return allocs


def is_settled(conn: sqlite3.Connection, signal_id: str, strategy_id: str) -> bool:
    return conn.execute("SELECT 1 FROM settlements WHERE signal_id=? AND strategy_id=?", (signal_id, strategy_id)).fetchone() is not None


def process_exit_batch(
    root: Path,
    mode: str,
    config_path: Path,
    signal_ids: list[str],
    books_by_token: dict[str, dict[str, Any]],
    source: str = "fixture",
    now: datetime | None = None,
    failpoints: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    config = load_config(config_path)
    db = init_ledger(root, mode, config_path)
    now = (now or utcnow()).astimezone(timezone.utc)
    conn = connect(db)
    results: list[dict[str, Any]] = []
    try:
        assert_formal_hashes(root, mode, config_path, conn)
        with conn:
            signals = sorted([get_signal(conn, sid) for sid in signal_ids], key=lambda r: (r["created_at_utc"], r["signal_id"]))
            for token_id in sorted({s["token_id"] for s in signals}):
                raw = books_by_token[token_id]
                book = normalize_book(raw)
                sid = record_snapshot(conn, token_id, "exit", raw, mode, source, now)
                bid, ask, spread = best_bid_ask(book)
                for strategy_id, strategy in STRATEGIES.items():
                    if strategy["multiple"] is None:
                        continue
                    bid_levels = [dict(level) for level in book["bids"]]
                    for sig in [s for s in signals if s["token_id"] == token_id]:
                        if is_settled(conn, sig["signal_id"], strategy_id):
                            continue
                        position = signal_position_conn(conn, sig["signal_id"], strategy_id)
                        if position["shares"] <= EPS:
                            continue
                        trigger = latest_trigger(conn, sig["signal_id"], strategy_id, strategy["stage"])
                        if trigger and trigger["trigger_status"] == "completed":
                            results.append({"signal_id": sig["signal_id"], "strategy_id": strategy_id, "status": "trigger_completed"})
                            continue
                        planned = trigger["trigger_remaining_shares"] if trigger else position["shares"] * float(strategy["fraction"])
                        threshold = trigger["threshold_price"] if trigger else position["avg_cost"] * float(strategy["multiple"])
                        if planned <= EPS:
                            continue
                        probe = probe_sell_depth([dict(level) for level in bid_levels], planned)
                        if probe["shares"] <= EPS or not math.isfinite(probe["vwap"]) or probe["vwap"] + EPS < threshold:
                            results.append({"signal_id": sig["signal_id"], "strategy_id": strategy_id, "status": "not_triggered", "threshold": threshold, "executable_vwap": probe["vwap"]})
                            continue
                        trigger = trigger or create_trigger(conn, sig, strategy_id, position, mode, now)
                        duplicate = conn.execute("SELECT 1 FROM exit_fills WHERE trigger_id=? AND strategy_id=? AND snapshot_id=?", (trigger["trigger_id"], strategy_id, sid)).fetchone()
                        if duplicate:
                            results.append({"signal_id": sig["signal_id"], "strategy_id": strategy_id, "status": "skipped_duplicate_snapshot"})
                            continue
                        planned = min(trigger["trigger_remaining_shares"], signal_position_conn(conn, sig["signal_id"], strategy_id)["shares"])
                        sell = consume_sell_depth(bid_levels, planned)
                        shares = min(sell["shares"], trigger["trigger_remaining_shares"])
                        if shares <= EPS:
                            continue
                        gross = sell["gross"] * (shares / sell["shares"])
                        exit_fee = fee(gross, fnum(config["fees"].get("exit_fee_bps", 0)))
                        fill_id = id_for("exit", {"trigger_id": trigger["trigger_id"], "snapshot_id": sid, "shares": shares})
                        conn.execute(
                            "INSERT INTO exit_fills(exit_fill_id,trigger_id,signal_id,strategy_id,trigger_stage_id,event_key,token_id,snapshot_id,filled_at_utc,planned_sell_shares,filled_shares,gross_exit_proceeds,exit_fee,net_exit_proceeds,exit_vwap,best_bid,best_ask,spread,complete_fill,unfilled_trigger_shares_after_fill,depth_levels_json,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (fill_id, trigger["trigger_id"], sig["signal_id"], strategy_id, trigger["trigger_stage_id"], sig["event_key"], sig["token_id"], sid, now.isoformat(), planned, shares, gross, exit_fee, gross - exit_fee, gross / shares, bid, ask, spread, int(shares >= planned - 1e-6), max(trigger["trigger_remaining_shares"] - shares, 0.0), stable_json(sell["levels"]), mode),
                        )
                        failpoint("after_exit_fill", failpoints)
                        for alloc in allocate_fifo(conn, sig["signal_id"], strategy_id, shares, gross, exit_fee):
                            conn.execute(
                                "INSERT INTO exit_fill_allocations(allocation_id,exit_fill_id,trigger_id,strategy_id,signal_id,event_key,token_id,lot_id,allocated_shares,gross_exit_proceeds,exit_fee,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                (id_for("alloc", {"exit_fill_id": fill_id, "lot_id": alloc["lot"]["lot_id"]}), fill_id, trigger["trigger_id"], strategy_id, sig["signal_id"], sig["event_key"], sig["token_id"], alloc["lot"]["lot_id"], alloc["shares"], alloc["gross"], alloc["fee"], mode),
                            )
                        update_trigger(conn, trigger, shares, mode, now)
                        results.append({"signal_id": sig["signal_id"], "strategy_id": strategy_id, "status": "exit_filled", "filled_shares": shares, "remaining_after": signal_position_conn(conn, sig["signal_id"], strategy_id)["shares"]})
        return results
    finally:
        conn.close()


def process_exit(root: Path, mode: str, config_path: Path, signal_id: str, raw_book: dict[str, Any], **kwargs) -> list[dict[str, Any]]:
    config = load_config(config_path)
    conn = connect(db_path(root, mode, config))
    try:
        token_id = get_signal(conn, signal_id)["token_id"]
    finally:
        conn.close()
    return process_exit_batch(root, mode, config_path, [signal_id], {token_id: raw_book}, **kwargs)


def settle(root: Path, mode: str, config_path: Path, settlements_file: Path, now: datetime | None = None, failpoints: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    config = load_config(config_path)
    db = init_ledger(root, mode, config_path)
    now = (now or utcnow()).astimezone(timezone.utc)
    rows = read_csv_rows(settlements_file)
    conn = connect(db)
    out: list[dict[str, Any]] = []
    try:
        assert_formal_hashes(root, mode, config_path, conn)
        with conn:
            for row in rows:
                sig = get_signal(conn, row["signal_id"])
                value = fnum(row.get("settlement_value"), math.nan)
                if not math.isfinite(value) or value < -EPS or value > 1 + EPS:
                    raise ValueError("settlement_value must be between 0 and 1")
                raw_response = row.get("raw_response", "")
                evidence_hash = row.get("evidence_hash") or sha256_text(raw_response)
                if raw_response and evidence_hash != sha256_text(raw_response):
                    raise RuntimeError("settlement evidence_hash mismatch")
                required = ["source_type", "source", "source_reference", "observed_at_utc", "settlement_outcome"]
                if any(not row.get(k) for k in required) or not raw_response:
                    raise ValueError("settlement evidence fields are required")
                parse_utc(row["observed_at_utc"], require_utc=True)
                for strategy_id in STRATEGY_IDS:
                    existing = conn.execute("SELECT * FROM settlements WHERE signal_id=? AND strategy_id=? ORDER BY row_id DESC LIMIT 1", (sig["signal_id"], strategy_id)).fetchone()
                    if existing:
                        if existing["settlement_outcome"] != row["settlement_outcome"] or abs(existing["settlement_value"] - value) > 1e-9:
                            append_safe_audit(root, mode, "settlement_conflict_rejected", {"signal_id": sig["signal_id"], "strategy_id": strategy_id}, config, now)
                            raise RuntimeError("conflicting settlement result")
                        continue
                    lots = lot_open_rows(conn, sig["signal_id"], strategy_id)
                    remaining = sum(l["open_shares"] for l in lots)
                    proceeds = remaining * value
                    set_fee = fee(proceeds, fnum(config["fees"].get("settlement_fee_bps", 0)))
                    settlement_id = id_for("set", {"signal_id": sig["signal_id"], "strategy_id": strategy_id, "value": value})
                    conn.execute(
                        "INSERT INTO settlements(settlement_id,signal_id,strategy_id,event_key,condition_id,token_id,source_type,source,source_reference,observed_at_utc,recorded_at_utc,raw_response,evidence_hash,settlement_outcome,settlement_value,operator_notes,settlement_status,remaining_shares_settled,settlement_proceeds,settlement_fee,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (settlement_id, sig["signal_id"], strategy_id, sig["event_key"], sig["condition_id"], sig["token_id"], row["source_type"], row["source"], row["source_reference"], row["observed_at_utc"], now.isoformat(), raw_response, evidence_hash, row["settlement_outcome"], value, row.get("operator_notes", ""), "final", remaining, proceeds, set_fee, mode),
                    )
                    failpoint("after_settlement", failpoints)
                    for lot in lots:
                        ratio = lot["open_shares"] / remaining if remaining > EPS else 0
                        conn.execute(
                            "INSERT INTO settlement_allocations(settlement_allocation_id,settlement_id,strategy_id,signal_id,event_key,token_id,lot_id,settled_shares,settlement_proceeds,settlement_fee,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (id_for("set_alloc", {"settlement_id": settlement_id, "lot_id": lot["lot_id"]}), settlement_id, strategy_id, sig["signal_id"], sig["event_key"], sig["token_id"], lot["lot_id"], lot["open_shares"], proceeds * ratio, set_fee * ratio, mode),
                        )
                    out.append({"signal_id": sig["signal_id"], "strategy_id": strategy_id, "settlement_id": settlement_id})
            failpoint("before_state_update", failpoints)
        aggregate_results(root, mode, config_path)
        return out
    finally:
        conn.close()


def aggregate_results(root: Path, mode: str, config_path: Path) -> list[dict[str, Any]]:
    config = load_config(config_path)
    conn = connect(db_path(root, mode, config))
    rows: list[dict[str, Any]] = []
    try:
        with conn:
            conn.execute("DELETE FROM event_results")
            event_keys = [r["event_key"] for r in conn.execute("SELECT DISTINCT event_key FROM signals ORDER BY event_key")]
            for event_key in event_keys:
                signals = conn.execute("SELECT * FROM signals WHERE event_key=?", (event_key,)).fetchall()
                traded_signal_ids = {r["signal_id"] for r in conn.execute("SELECT DISTINCT signal_id FROM entry_fills WHERE event_key=?", (event_key,))}
                for strategy_id in STRATEGY_IDS:
                    gross_entry = conn.execute("SELECT COALESCE(SUM(gross_entry_cost),0) v FROM entry_fills WHERE event_key=?", (event_key,)).fetchone()["v"]
                    entry_fee = conn.execute("SELECT COALESCE(SUM(entry_fee),0) v FROM entry_fills WHERE event_key=?", (event_key,)).fetchone()["v"]
                    gross_exit = conn.execute("SELECT COALESCE(SUM(gross_exit_proceeds),0) v FROM exit_fill_allocations WHERE event_key=? AND strategy_id=?", (event_key, strategy_id)).fetchone()["v"]
                    exit_fee = conn.execute("SELECT COALESCE(SUM(exit_fee),0) v FROM exit_fill_allocations WHERE event_key=? AND strategy_id=?", (event_key, strategy_id)).fetchone()["v"]
                    set_proceeds = conn.execute("SELECT COALESCE(SUM(settlement_proceeds),0) v FROM settlement_allocations WHERE event_key=? AND strategy_id=?", (event_key, strategy_id)).fetchone()["v"]
                    set_fee = conn.execute("SELECT COALESCE(SUM(settlement_fee),0) v FROM settlement_allocations WHERE event_key=? AND strategy_id=?", (event_key, strategy_id)).fetchone()["v"]
                    settled_ids = {r["signal_id"] for r in conn.execute("SELECT DISTINCT signal_id FROM settlements WHERE event_key=? AND strategy_id=?", (event_key, strategy_id))}
                    settled_event = int(bool(traded_signal_ids) and all(sid in settled_ids for sid in traded_signal_ids))
                    total_fees = entry_fee + exit_fee + set_fee
                    gross_pnl = gross_exit + set_proceeds - gross_entry
                    net_pnl = gross_pnl - total_fees
                    latest_triggers = conn.execute(
                        """
                        SELECT t.* FROM strategy_triggers t
                        JOIN (SELECT trigger_id, MAX(row_id) AS max_row_id FROM strategy_triggers GROUP BY trigger_id) latest ON latest.max_row_id=t.row_id
                        WHERE t.event_key=? AND t.strategy_id=?
                        """,
                        (event_key, strategy_id),
                    ).fetchall()
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
                        "settlement_proceeds": set_proceeds,
                        "settlement_fee": set_fee,
                        "total_fees": total_fees,
                        "gross_pnl": gross_pnl if settled_event else None,
                        "net_pnl": net_pnl if settled_event else None,
                        "triggered_take_profit": int(conn.execute("SELECT 1 FROM exit_fill_allocations WHERE event_key=? AND strategy_id=? LIMIT 1", (event_key, strategy_id)).fetchone() is not None),
                        "incomplete_take_profit": int(any(t["trigger_remaining_shares"] > EPS for t in latest_triggers)),
                    }
                    conn.execute(
                        "INSERT INTO event_results(event_key,strategy_id,mode,signal_count,position_count,traded_event_count,settled_event_count,gross_entry_cost,entry_fee,gross_exit_proceeds,exit_fee,settlement_proceeds,settlement_fee,total_fees,gross_pnl,net_pnl,triggered_take_profit,incomplete_take_profit) VALUES(:event_key,:strategy_id,:mode,:signal_count,:position_count,:traded_event_count,:settled_event_count,:gross_entry_cost,:entry_fee,:gross_exit_proceeds,:exit_fee,:settlement_proceeds,:settlement_fee,:total_fees,:gross_pnl,:net_pnl,:triggered_take_profit,:incomplete_take_profit)",
                        row,
                    )
                    rows.append(row)
        return rows
    finally:
        conn.close()


def compute_counts(conn: sqlite3.Connection) -> dict[str, int]:
    signals = conn.execute("SELECT * FROM signals").fetchall()
    traded_signals = {r["signal_id"] for r in conn.execute("SELECT DISTINCT signal_id FROM entry_fills")}
    traded_events = {r["event_key"] for r in conn.execute("SELECT DISTINCT event_key FROM entry_fills")}
    settled_position_count = 0
    for sid in traded_signals:
        if all(conn.execute("SELECT 1 FROM settlements WHERE signal_id=? AND strategy_id=?", (sid, st)).fetchone() for st in STRATEGY_IDS):
            settled_position_count += 1
    settled_event_count = 0
    for event_key in traded_events:
        event_sids = {r["signal_id"] for r in conn.execute("SELECT DISTINCT signal_id FROM entry_fills WHERE event_key=?", (event_key,))}
        if event_sids and all(all(conn.execute("SELECT 1 FROM settlements WHERE signal_id=? AND strategy_id=?", (sid, st)).fetchone() for st in STRATEGY_IDS) for sid in event_sids):
            settled_event_count += 1
    return {
        "registered_signal_count": len(signals),
        "traded_position_count": len(traded_signals),
        "traded_event_count": len(traded_events),
        "settled_position_count": settled_position_count,
        "settled_event_count": settled_event_count,
        "remaining_to_50_settled_events": max(50 - settled_event_count, 0),
    }


def status(root: Path, mode: str, config_path: Path) -> dict[str, Any]:
    config = load_config(config_path)
    db = init_ledger(root, mode, config_path)
    conn = connect(db)
    try:
        counts = compute_counts(conn)
        hash_match = True
        if mode == FORMAL and get_state(conn, "formal_started_at_utc", ""):
            cur = current_hashes(root, config_path)
            hash_match = all(get_state(conn, k, "") == v for k, v in cur.items())
        return {
            "version": VERSION,
            "schema_version": get_state(conn, "schema_version", SCHEMA_VERSION),
            "mode": mode,
            "config_path": str(config_path),
            "ledger_path": str(db),
            "formal_started_at_utc": get_state(conn, "formal_started_at_utc", ""),
            "paused": get_state(conn, "paused", "false") == "true",
            "stopped": get_state(conn, "stopped", "false") == "true",
            "hash_match": hash_match,
            **counts,
        }
    finally:
        conn.close()


def pause_resume_stop(root: Path, mode: str, config_path: Path, action: str, now: datetime | None = None, failpoints: dict[str, Any] | None = None) -> dict[str, Any]:
    config = load_config(config_path)
    db = init_ledger(root, mode, config_path)
    conn = connect(db)
    try:
        assert_formal_hashes(root, mode, config_path, conn)
        with conn:
            if action == "pause":
                set_state(conn, "paused", "true")
            elif action == "resume":
                assert_formal_hashes(root, mode, config_path, conn)
                set_state(conn, "paused", "false")
                set_state(conn, "stopped", "false")
            elif action == "stop":
                set_state(conn, "stopped", "true")
            failpoint("before_state_update", failpoints)
        append_safe_audit(root, mode, f"state_{action}", {"action": action}, config, now)
        return {"status": action}
    finally:
        conn.close()


class LedgerLock:
    def __init__(self, root: Path, mode: str, config: dict[str, Any], stale_seconds: float | None = None):
        self.path = data_dir(root, mode, config) / ".run_loop.lock"
        self.stale_seconds = stale_seconds if stale_seconds is not None else fnum(config.get("execution", {}).get("lock_stale_seconds", 300))
        self.acquired = False

    def __enter__(self):
        if self.path.exists():
            created_file = self.path / "created_at_epoch"
            age = time.time() - fnum(created_file.read_text(encoding="utf-8") if created_file.exists() else "0")
            if age > self.stale_seconds:
                shutil.rmtree(self.path)
            else:
                raise RuntimeError("another run-loop instance already holds the lock")
        self.path.mkdir(parents=True)
        (self.path / "pid").write_text(str(os.getpid()), encoding="utf-8")
        (self.path / "created_at_epoch").write_text(str(time.time()), encoding="utf-8")
        self.acquired = True
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.acquired and self.path.exists():
            shutil.rmtree(self.path)


def run_loop(
    root: Path,
    mode: str,
    config_path: Path,
    book_provider,
    iterations: int = 1,
    sleep_seconds: float = 0.0,
    now: datetime | None = None,
) -> dict[str, Any]:
    config = load_config(config_path)
    init_ledger(root, mode, config_path)
    completed = 0
    with LedgerLock(root, mode, config):
        while iterations <= 0 or completed < iterations:
            conn = connect(db_path(root, mode, config))
            try:
                if get_state(conn, "stopped", "false") == "true":
                    break
                if get_state(conn, "paused", "false") == "true":
                    append_safe_audit(root, mode, "heartbeat", {"status": "paused", "iteration": completed + 1}, config, now)
                    completed += 1
                    continue
                signals = list_active_entry_signals(conn)
                active_ids = [s["signal_id"] for s in signals]
                open_signal_ids = {r["signal_id"] for r in conn.execute("SELECT DISTINCT signal_id FROM strategy_lots")}
                active_ids = sorted(set(active_ids) | open_signal_ids)
            finally:
                conn.close()
            books: dict[str, dict[str, Any]] = {}
            for sid in active_ids:
                conn = connect(db_path(root, mode, config))
                try:
                    sig = get_signal(conn, sid)
                    token = sig["token_id"]
                finally:
                    conn.close()
                if token not in books:
                    try:
                        books[token] = book_provider(token, "entry")
                    except Exception as exc:
                        append_safe_audit(root, mode, "market_loop_error", {"token_id": token, "purpose": "entry", "error": str(exc)}, config, now)
            if books and active_ids:
                try:
                    entry_ids = []
                    for sid in active_ids:
                        conn = connect(db_path(root, mode, config))
                        try:
                            if get_signal(conn, sid)["token_id"] in books:
                                entry_ids.append(sid)
                        finally:
                            conn.close()
                    if entry_ids:
                        process_entry_batch(root, mode, config_path, entry_ids, books, "run_loop", now)
                except Exception as exc:
                    append_safe_audit(root, mode, "market_loop_error", {"stage": "entry", "error": str(exc)}, config, now)
            exit_books: dict[str, dict[str, Any]] = {}
            for sid in active_ids:
                conn = connect(db_path(root, mode, config))
                try:
                    sig = get_signal(conn, sid)
                    token = sig["token_id"]
                finally:
                    conn.close()
                if token not in exit_books:
                    try:
                        exit_books[token] = book_provider(token, "exit")
                    except Exception as exc:
                        append_safe_audit(root, mode, "market_loop_error", {"token_id": token, "purpose": "exit", "error": str(exc)}, config, now)
            if exit_books and active_ids:
                try:
                    exit_ids = []
                    for sid in active_ids:
                        conn = connect(db_path(root, mode, config))
                        try:
                            if get_signal(conn, sid)["token_id"] in exit_books:
                                exit_ids.append(sid)
                        finally:
                            conn.close()
                    if exit_ids:
                        process_exit_batch(root, mode, config_path, exit_ids, exit_books, "run_loop", now)
                except Exception as exc:
                    append_safe_audit(root, mode, "market_loop_error", {"stage": "exit", "error": str(exc)}, config, now)
            append_safe_audit(root, mode, "heartbeat", {"status": "ok", "iteration": completed + 1}, config, now)
            completed += 1
            if sleep_seconds:
                time.sleep(sleep_seconds)
    return {"iterations_completed": completed}


def table_rows(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    return conn.execute(f"SELECT * FROM {table}").fetchall()


def raw_snapshot_depth(snapshot: sqlite3.Row, side: str) -> float:
    raw = json.loads(snapshot["raw_orderbook_json"])
    book = normalize_book(raw)
    return sum(level["size"] for level in book[side])


def audit_integrity(root: Path, mode: str, config_path: Path) -> dict[str, Any]:
    config = load_config(config_path)
    db = init_ledger(root, mode, config_path)
    conn = connect(db)
    checks: dict[str, Any] = {}
    try:
        signals = table_rows(conn, "signals")
        entry_fills = table_rows(conn, "entry_fills")
        exit_fills = table_rows(conn, "exit_fills")
        snapshots = table_rows(conn, "orderbook_snapshots")
        triggers_latest = {
            r["trigger_id"]: r
            for r in conn.execute(
                "SELECT t.* FROM strategy_triggers t JOIN (SELECT trigger_id, MAX(row_id) AS max_row_id FROM strategy_triggers GROUP BY trigger_id) latest ON latest.max_row_id=t.row_id"
            )
        }
        settlements = table_rows(conn, "settlements")
        duplicate = lambda rows, key: len([r[key] for r in rows]) - len({r[key] for r in rows})
        checks["duplicate_signal_id"] = duplicate(signals, "signal_id")
        checks["duplicate_fill_id"] = len([r["entry_fill_id"] for r in entry_fills] + [r["exit_fill_id"] for r in exit_fills]) - len(set([r["entry_fill_id"] for r in entry_fills] + [r["exit_fill_id"] for r in exit_fills]))
        checks["duplicate_snapshot_id"] = duplicate(snapshots, "snapshot_id")
        safe_events = read_safe_audit(root, mode, config)
        registered_hashes = {(e["payload"].get("signal_id"), e["payload"].get("signal_hash")) for e in safe_events if e.get("event_type") == "signal_registered"}
        checks["unregistered_signal_rows"] = sum(1 for s in signals if (s["signal_id"], s["signal_hash"]) not in registered_hashes)
        negative = over_sell = 0
        for sig in signals:
            for strategy_id in STRATEGY_IDS:
                bought = conn.execute("SELECT COALESCE(SUM(entry_shares),0) v FROM strategy_lots WHERE signal_id=? AND strategy_id=?", (sig["signal_id"], strategy_id)).fetchone()["v"]
                sold = conn.execute("SELECT COALESCE(SUM(allocated_shares),0) v FROM exit_fill_allocations WHERE signal_id=? AND strategy_id=?", (sig["signal_id"], strategy_id)).fetchone()["v"]
                settled = conn.execute("SELECT COALESCE(SUM(settled_shares),0) v FROM settlement_allocations WHERE signal_id=? AND strategy_id=?", (sig["signal_id"], strategy_id)).fetchone()["v"]
                if bought - sold - settled < -1e-6:
                    negative += 1
                if sold + settled - bought > 1e-6:
                    over_sell += 1
        checks["negative_inventory"] = negative
        checks["over_sell"] = over_sell
        checks["trigger_overfill"] = sum(1 for t in triggers_latest.values() if t["trigger_filled_shares"] - t["trigger_target_shares"] > 1e-6)
        inconsistent = 0
        for sig in signals:
            shares = [conn.execute("SELECT COALESCE(SUM(entry_shares),0) v FROM strategy_lots WHERE signal_id=? AND strategy_id=?", (sig["signal_id"], st)).fetchone()["v"] for st in STRATEGY_IDS]
            if shares and max(shares) - min(shares) > 1e-6:
                inconsistent += 1
        checks["strategy_entry_inconsistent"] = inconsistent
        checks["demo_pollution_formal"] = int(mode == FORMAL and any(r["mode"] == DEMO for r in signals + entry_fills + exit_fills + settlements))
        hash_errors = []
        if mode == FORMAL and get_state(conn, "formal_started_at_utc", ""):
            try:
                cur = current_hashes(root, config_path)
                hash_errors = [k for k, v in cur.items() if get_state(conn, k, "") != v]
            except Exception as exc:
                hash_errors = [str(exc)]
        checks["hash_drift"] = len(hash_errors)
        settled_times = {(r["signal_id"], r["strategy_id"]): parse_utc(r["recorded_at_utc"]) for r in settlements}
        checks["settled_after_exit"] = sum(1 for ex in exit_fills if (ex["signal_id"], ex["strategy_id"]) in settled_times and parse_utc(ex["filled_at_utc"]) > settled_times[(ex["signal_id"], ex["strategy_id"])])
        timestamp_order = 0
        sig_created = {s["signal_id"]: parse_utc(s["created_at_utc"]) for s in signals}
        for fill in entry_fills:
            if fill["signal_id"] in sig_created and parse_utc(fill["filled_at_utc"]) < sig_created[fill["signal_id"]]:
                timestamp_order += 1
        for ex in exit_fills:
            if ex["signal_id"] in sig_created and parse_utc(ex["filled_at_utc"]) < sig_created[ex["signal_id"]]:
                timestamp_order += 1
        checks["timestamp_order"] = timestamp_order
        formal_timeout = 0
        if mode == FORMAL:
            max_delay = fnum(config["sample_rules"].get("max_signal_registration_delay_seconds", 300))
            for sig in signals:
                if (parse_utc(sig["registered_at_utc"]) - parse_utc(sig["created_at_utc"])).total_seconds() > max_delay:
                    formal_timeout += 1
        checks["formal_signal_registration_timeout"] = formal_timeout
        missing_evidence = evidence_mismatch = 0
        seen_settlement_results: dict[tuple[str, str], tuple[str, float]] = {}
        duplicate_settlement_result = conflicting_settlement_result = 0
        for s in settlements:
            if not all(s[k] for k in ["source_type", "source", "source_reference", "observed_at_utc", "raw_response", "evidence_hash", "settlement_outcome"]):
                missing_evidence += 1
            if s["raw_response"] and sha256_text(s["raw_response"]) != s["evidence_hash"]:
                evidence_mismatch += 1
            key = (s["signal_id"], s["strategy_id"])
            value = (s["settlement_outcome"], round(s["settlement_value"], 10))
            if key in seen_settlement_results:
                if seen_settlement_results[key] == value:
                    duplicate_settlement_result += 1
                else:
                    conflicting_settlement_result += 1
            seen_settlement_results[key] = value
        checks["missing_settlement_evidence"] = missing_evidence
        checks["evidence_hash_mismatch"] = evidence_mismatch
        checks["duplicate_settlement_result"] = duplicate_settlement_result
        checks["conflicting_settlement_result"] = conflicting_settlement_result
        repeated_depth = 0
        snapshot_by_id = {s["snapshot_id"]: s for s in snapshots}
        for snapshot_id, snap in snapshot_by_id.items():
            if snap["purpose"] == "entry":
                filled = conn.execute("SELECT COALESCE(SUM(filled_shares),0) v FROM entry_fills WHERE snapshot_id=?", (snapshot_id,)).fetchone()["v"]
                if filled - raw_snapshot_depth(snap, "asks") > 1e-6:
                    repeated_depth += 1
            if snap["purpose"] == "exit":
                for strategy_id in STRATEGY_IDS:
                    filled = conn.execute("SELECT COALESCE(SUM(filled_shares),0) v FROM exit_fills WHERE snapshot_id=? AND strategy_id=?", (snapshot_id, strategy_id)).fetchone()["v"]
                    if filled - raw_snapshot_depth(snap, "bids") > 1e-6:
                        repeated_depth += 1
        checks["repeated_orderbook_depth"] = repeated_depth
        try:
            stored_before = [dict(r) for r in conn.execute("SELECT * FROM event_results")]
            computed = aggregate_results(root, mode, config_path)
            stored = stored_before or [dict(r) for r in conn.execute("SELECT * FROM event_results")]
            mismatch = 0
            by_key = {(r["event_key"], r["strategy_id"]): r for r in computed}
            for row in stored:
                c = by_key.get((row["event_key"], row["strategy_id"]))
                if not c:
                    mismatch += 1
                else:
                    for key in ["gross_entry_cost", "entry_fee", "gross_exit_proceeds", "exit_fee", "settlement_proceeds", "settlement_fee", "total_fees"]:
                        if abs(fnum(row[key]) - fnum(c[key])) > 1e-6:
                            mismatch += 1
                            break
            checks["event_result_mismatch"] = mismatch
        except Exception as exc:
            checks["event_result_mismatch"] = 1
            checks["event_result_error"] = str(exc)
        ok = not any(v for k, v in checks.items() if isinstance(v, (int, float)) and v)
        return {"ok": ok, "checks": checks, "hash_errors": hash_errors}
    finally:
        conn.close()


def write_signal_template(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = USER_SIGNAL_FIELDS
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_settlement_file(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = ["signal_id", "condition_id", "token_id", "source_type", "source", "source_reference", "observed_at_utc", "raw_response", "evidence_hash", "settlement_outcome", "settlement_value", "operator_notes"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    p.add_argument("--root", default=".")
    p.add_argument("--config", required=True)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init").add_argument("--mode", choices=[FORMAL, DEMO], default=DEMO)
    sp = sub.add_parser("start-formal"); sp.add_argument("--confirm", action="store_true")
    sp = sub.add_parser("register"); sp.add_argument("--signals-file", required=True); sp.add_argument("--mode", choices=[FORMAL, DEMO], default=FORMAL)
    sp = sub.add_parser("status"); sp.add_argument("--mode", choices=[FORMAL, DEMO], default=FORMAL)
    sp = sub.add_parser("audit-integrity"); sp.add_argument("--mode", choices=[FORMAL, DEMO], default=FORMAL)
    sp = sub.add_parser("pause"); sp.add_argument("--mode", choices=[FORMAL, DEMO], default=FORMAL)
    sp = sub.add_parser("resume"); sp.add_argument("--mode", choices=[FORMAL, DEMO], default=FORMAL)
    sp = sub.add_parser("stop"); sp.add_argument("--mode", choices=[FORMAL, DEMO], default=FORMAL)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    root = Path(args.root)
    config_path = Path(args.config)
    if args.command == "init":
        print(json.dumps({"ledger": str(init_ledger(root, args.mode, config_path))}, indent=2, ensure_ascii=False))
    elif args.command == "start-formal":
        print(json.dumps(start_formal(root, config_path, args.confirm), indent=2, ensure_ascii=False))
    elif args.command == "register":
        rows = register_signals(root, args.mode, config_path, Path(args.signals_file))
        print(json.dumps({"registered": len(rows)}, indent=2, ensure_ascii=False))
    elif args.command == "status":
        print(json.dumps(status(root, args.mode, config_path), indent=2, ensure_ascii=False, sort_keys=True))
    elif args.command == "audit-integrity":
        print(json.dumps(audit_integrity(root, args.mode, config_path), indent=2, ensure_ascii=False, sort_keys=True))
    elif args.command in {"pause", "resume", "stop"}:
        print(json.dumps(pause_resume_stop(root, args.mode, config_path, args.command), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
