#!/usr/bin/env python3
"""SQLite-backed weather forward simulation v5.1.8-RC7.

This module is standalone. It does not import v5, v5.1, v5.1.1, or v5.1.2.
Formal monitor commands use the v5.1.8 public adapter as the only live price
source and never use historical prices or page-displayed probabilities.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_EVEN, getcontext
from pathlib import Path
from typing import Any

try:
    from src.polymarket_public_adapter_v5_1_8 import (
        ADAPTER_NAME,
        ADAPTER_VERSION,
        CLOB_BASE,
        FILL_ALGORITHM_VERSION,
        GAMMA_BASE,
        NORMALIZED_BOOK_ALGORITHM_VERSION,
        AdapterError,
        HttpResult,
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
        market_state,
        normalize_orderbook,
        parse_settlement_evidence,
        parse_temperature_bucket,
        parse_temperature_bucket_info,
        parse_weather_market,
        persist_http_result,
        stable_json,
        validate_token_mapping,
        verify_http_evidence_file,
        write_json,
    )
except ModuleNotFoundError:
    from polymarket_public_adapter_v5_1_8 import (
        ADAPTER_NAME,
        ADAPTER_VERSION,
        CLOB_BASE,
        FILL_ALGORITHM_VERSION,
        GAMMA_BASE,
        NORMALIZED_BOOK_ALGORITHM_VERSION,
        AdapterError,
        HttpResult,
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
        market_state,
        normalize_orderbook,
        parse_settlement_evidence,
        parse_temperature_bucket,
        parse_temperature_bucket_info,
        parse_weather_market,
        persist_http_result,
        stable_json,
        validate_token_mapping,
        verify_http_evidence_file,
        write_json,
    )


getcontext().prec = 28

VERSION = "forward_simulation_v5.1.8-rc7"
SCHEMA_VERSION = "forward_v5_1_8_schema_001"
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
    "bucket_type",
    "temperature_threshold",
    "temperature_unit",
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
    "entry_deadline_utc",
]

CANONICAL_SIGNAL_FIELDS = [
    "signal_id",
    "created_at_utc",
    "city",
    "weather_date_local",
    "weather_metric",
    "bucket_type",
    "temperature_threshold",
    "temperature_unit",
    "market_slug",
    "condition_id",
    "token_id",
    "outcome",
    "side",
    "intended_usd",
    "max_entry_price",
    "forecast_probability",
    "market_probability_at_signal",
    "source",
    "notes",
    "entry_deadline_utc",
]

HASH_FILES = {
    "config_sha256": "config/forward_simulation_v5_1_8.yaml",
    "core_code_sha256": "src/forward_simulation_v5_1_8.py",
    "adapter_code_sha256": "src/polymarket_public_adapter_v5_1_8.py",
    "reporting_code_sha256": "src/forward_reporting_v5_1_8.py",
    "schema_sha256": "schemas/forward_simulation_v5_1_8.sql",
    "preregistration_sha256": "reports/FORWARD_SIMULATION_V5_1_8_PREREGISTRATION.md",
    "api_contract_sha256": "reports/FORWARD_SIMULATION_V5_1_8_API_CONTRACT.md",
    "fee_contract_sha256": "reports/FORWARD_SIMULATION_V5_1_8_FEE_CONTRACT.md",
    "settlement_finality_contract_sha256": "reports/FORWARD_SIMULATION_V5_1_8_SETTLEMENT_FINALITY_CONTRACT.md",
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


def parse_strict_utc_literal(value: str) -> datetime:
    raw = str(value or "")
    if not (raw.endswith("+00:00") or raw.endswith("Z")):
        raise ValueError("timestamp must be an explicit UTC literal ending in Z or +00:00")
    return parse_utc(raw, require_utc=True)


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
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def repository_relative_path(root: Path, path: Path | str) -> str:
    candidate = Path(path)
    root_res = Path(root).resolve()
    if not candidate.is_absolute():
        return candidate.as_posix()
    try:
        return candidate.resolve().relative_to(root_res).as_posix()
    except Exception:
        return str(path)


def portable_path_fields(root: Path, path: Path | str) -> dict[str, Any]:
    candidate = Path(path)
    root_res = Path(root).resolve()
    if not candidate.is_absolute():
        return {
            "repository_relative_path": candidate.as_posix(),
            "historical_local_path_nonportable": False,
        }
    try:
        rel = candidate.resolve().relative_to(root_res).as_posix()
        return {
            "repository_relative_path": rel,
            "historical_local_path_nonportable": False,
        }
    except Exception:
        return {
            "repository_relative_path": None,
            "historical_local_path": str(path),
            "historical_local_path_nonportable": True,
        }


def sanitize_status_for_release(root: Path, st: dict[str, Any]) -> dict[str, Any]:
    out = dict(st)
    for key in ("config_path", "ledger_path"):
        if key in out and out[key]:
            fields = portable_path_fields(root, out[key])
            if fields.get("repository_relative_path"):
                out[key] = fields["repository_relative_path"]
            out[f"{key}_meta"] = fields
    return out


def data_dir(root: Path, mode: str, config: dict[str, Any]) -> Path:
    if mode == LIVE:
        return root / str(config["paths"].get("live_integration_dir", "data/forward_v5_1_8/live_integration"))
    return root / str(config["paths"].get(f"{mode}_data_dir", f"data/forward_v5_1_8/{mode}"))


def db_path(root: Path, mode: str, config: dict[str, Any]) -> Path:
    return data_dir(root, mode, config) / "ledger.sqlite3"


def rc7_dir(root: Path) -> Path:
    return root / "data/forward_v5_1_8/rc7"


def lock_path(root: Path, mode: str, config: dict[str, Any]) -> Path:
    return data_dir(root, mode, config) / "monitor.lock.json"


def heartbeat_path(root: Path, mode: str, config: dict[str, Any]) -> Path:
    return data_dir(root, mode, config) / "heartbeat.json"


def schema_path(root: Path, config: dict[str, Any]) -> Path:
    p = root / str(config["paths"].get("schema_file", "schemas/forward_simulation_v5_1_8.sql"))
    return p if p.exists() else PROJECT_ROOT / str(config["paths"].get("schema_file", "schemas/forward_simulation_v5_1_8.sql"))


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


def read_json_if_exists(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def os_process_start_time(pid: int | None) -> str | None:
    if not pid_alive(pid):
        return None
    try:
        import psutil  # type: ignore

        return datetime.fromtimestamp(psutil.Process(int(pid)).create_time(), timezone.utc).isoformat()
    except Exception:
        pass
    try:
        proc = subprocess.run(["ps", "-p", str(int(pid)), "-o", "lstart="], check=False, capture_output=True, text=True, timeout=2)
        raw = proc.stdout.strip()
        if proc.returncode != 0 or not raw:
            return None
        parsed = datetime.strptime(raw, "%a %b %d %H:%M:%S %Y")
        return parsed.astimezone().astimezone(timezone.utc).isoformat()
    except Exception:
        return None


PROCESS_START_TIME = os_process_start_time(os.getpid()) or utcnow().isoformat()


def process_start_matches(pid: int | None, recorded: str) -> bool:
    if not pid_alive(pid) or not recorded:
        return False
    actual = os_process_start_time(pid)
    return bool(actual and actual == recorded)


def lock_recovery_decision(info: dict[str, Any], config: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    stale_seconds = int(config.get("execution", {}).get("lock_stale_seconds", 300))
    heartbeat_at = str(info.get("heartbeat_at_utc") or info.get("created_at_utc") or "")
    heartbeat_stale = False
    if heartbeat_at:
        try:
            heartbeat_stale = ((now or utcnow()).astimezone(timezone.utc) - parse_utc(heartbeat_at)).total_seconds() > stale_seconds
        except Exception:
            heartbeat_stale = True
    else:
        heartbeat_stale = True
    hostname = str(info.get("hostname") or "")
    if hostname and hostname != socket.gethostname():
        return {"recoverable": False, "reason": "different_hostname", "heartbeat_stale": heartbeat_stale, "pid_alive": None, "process_start_match": None}
    pid = int(info.get("pid") or 0)
    alive = pid_alive(pid)
    recorded_start = str(info.get("process_start_time") or "")
    actual_start = os_process_start_time(pid) if alive else None
    if alive and actual_start is None:
        return {"recoverable": False, "reason": "process_start_unreadable", "heartbeat_stale": heartbeat_stale, "pid_alive": True, "process_start_match": None}
    if alive and actual_start == recorded_start:
        return {"recoverable": False, "reason": "active_pid", "heartbeat_stale": heartbeat_stale, "pid_alive": True, "process_start_match": True}
    if not heartbeat_stale:
        return {"recoverable": False, "reason": "heartbeat_not_stale", "heartbeat_stale": False, "pid_alive": alive, "process_start_match": bool(alive and actual_start == recorded_start)}
    if not alive:
        return {"recoverable": True, "reason": "pid_dead_stale", "heartbeat_stale": True, "pid_alive": False, "process_start_match": False}
    return {"recoverable": True, "reason": "pid_reused_stale", "heartbeat_stale": True, "pid_alive": True, "process_start_match": False, "actual_process_start_time": actual_start}


def write_monitor_heartbeat(root: Path, mode: str, config: dict[str, Any], run_id: str, status_value: str, now: datetime | None = None) -> dict[str, Any]:
    payload = {
        "version": VERSION,
        "mode": mode,
        "run_id": run_id,
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "process_start_time": PROCESS_START_TIME,
        "status": status_value,
        "heartbeat_at_utc": now_iso(now),
    }
    path = heartbeat_path(root, mode, config)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, payload)
    lock = lock_path(root, mode, config)
    if lock.exists():
        info = read_json_if_exists(lock)
        if info.get("pid") == os.getpid() and info.get("run_id") == run_id:
            info.update({"status": status_value, "heartbeat_at_utc": payload["heartbeat_at_utc"]})
            write_json(lock, info)
    return payload


def lock_is_stale(info: dict[str, Any], config: dict[str, Any], now: datetime | None = None) -> bool:
    return bool(lock_recovery_decision(info, config, now).get("recoverable"))


@contextmanager
def acquire_monitor_lock(root: Path, mode: str, config: dict[str, Any], run_id: str, command: str = "monitor", recover_stale_lock: bool = False, now: datetime | None = None, config_hash: str = "", code_hash: str = ""):
    lock = lock_path(root, mode, config)
    lock.parent.mkdir(parents=True, exist_ok=True)
    existing = read_json_if_exists(lock)
    if existing:
        decision = lock_recovery_decision(existing, config, now)
        if recover_stale_lock and decision.get("recoverable"):
            lock.unlink(missing_ok=True)
        else:
            raise RuntimeError(f"monitor lock is already held: {lock}; recovery_decision={stable_json(decision)}")
    payload = {
        "lock_id": id_for("lock", {"mode": mode, "run_id": run_id, "pid": os.getpid(), "command": command, "at": now_iso(now)}),
        "nonce": uuid.uuid4().hex,
        "version": VERSION,
        "mode": mode,
        "environment": mode,
        "command": command,
        "run_id": run_id,
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "process_start_time": PROCESS_START_TIME,
        "created_at_utc": now_iso(now),
        "acquired_at_utc": now_iso(now),
        "heartbeat_at_utc": now_iso(now),
        "code_hash": code_hash,
        "config_hash": config_hash,
        "status": "running",
    }
    fd: int | None = None
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.write(fd, (stable_json(payload) + "\n").encode("utf-8"))
        os.close(fd)
        fd = None
        write_monitor_heartbeat(root, mode, config, run_id, "running", now)
        current = read_json_if_exists(lock)
        if current.get("lock_id") == payload["lock_id"]:
            current["nonce"] = payload["nonce"]
            write_json(lock, current)
        yield payload
    finally:
        if fd is not None:
            os.close(fd)
        current = read_json_if_exists(lock)
        if current.get("pid") == os.getpid() and current.get("run_id") == run_id:
            lock.unlink(missing_ok=True)


def validate_lock_token(root: Path, mode: str, config: dict[str, Any], run_id: str, lock_token: dict[str, Any] | None) -> None:
    if not lock_token:
        raise RuntimeError("valid lock_token is required")
    required = ["nonce", "lock_id", "run_id", "pid", "process_start_time", "hostname"]
    missing = [key for key in required if not lock_token.get(key)]
    if missing:
        raise RuntimeError(f"invalid lock_token missing fields: {missing}")
    lock = read_json_if_exists(lock_path(root, mode, config))
    if not lock:
        raise RuntimeError("lock file is missing for lock_token")
    for key in required + ["mode"]:
        if str(lock.get(key) or "") != str(lock_token.get(key) or ""):
            raise RuntimeError(f"lock_token mismatch: {key}")
    if str(lock_token.get("run_id")) != run_id:
        raise RuntimeError("lock_token run_id mismatch")
    if str(lock_token.get("hostname")) != socket.gethostname():
        raise RuntimeError("lock_token hostname mismatch")
    if int(lock_token.get("pid")) != os.getpid():
        raise RuntimeError("lock_token pid mismatch")
    if not process_start_matches(os.getpid(), str(lock_token.get("process_start_time") or "")):
        raise RuntimeError("lock_token process_start_time mismatch")


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


def frozen_file_records(root: Path, config_path: Path) -> dict[str, dict[str, Any]]:
    config = load_config(config_path)
    paths = {
        "config_sha256": config_path,
        "core_code_sha256": root / "src/forward_simulation_v5_1_8.py",
        "adapter_code_sha256": root / "src/polymarket_public_adapter_v5_1_8.py",
        "reporting_code_sha256": root / "src/forward_reporting_v5_1_8.py",
        "schema_sha256": schema_path(root, config),
        "preregistration_sha256": root / str(config["paths"].get("preregistration_file", "reports/FORWARD_SIMULATION_V5_1_8_PREREGISTRATION.md")),
        "api_contract_sha256": root / "reports/FORWARD_SIMULATION_V5_1_8_API_CONTRACT.md",
        "fee_contract_sha256": root / "reports/FORWARD_SIMULATION_V5_1_8_FEE_CONTRACT.md",
        "settlement_finality_contract_sha256": root / "reports/FORWARD_SIMULATION_V5_1_8_SETTLEMENT_FINALITY_CONTRACT.md",
    }
    records: dict[str, dict[str, Any]] = {}
    for key, path in paths.items():
        portable = portable_path_fields(root, path)
        rel = portable.get("repository_relative_path") or str(path)
        if not path.exists():
            records[key] = {"path": rel, "sha256": None, "size": None, "missing": True, **portable}
        else:
            records[key] = {"path": rel, "sha256": sha256_bytes(path), "size": path.stat().st_size, "missing": False, **portable}
    return records


def current_hashes(root: Path, config_path: Path) -> dict[str, str]:
    return {k: str(v["sha256"]) for k, v in frozen_file_records(root, config_path).items() if not v["missing"]}


def adapter_code_hash(root: Path, config_path: Path) -> str:
    return current_hashes(root, config_path).get("adapter_code_sha256", "")


def source_meta(root: Path, config_path: Path, mode: str, source_endpoint: str, raw_hash: str) -> dict[str, str]:
    return {
        "adapter_name": ADAPTER_NAME if mode == FORMAL else "FixtureAdapter" if source_endpoint == "fixture" or source_endpoint.startswith("fixture") else ADAPTER_NAME,
        "adapter_version": ADAPTER_VERSION,
        "adapter_code_hash": adapter_code_hash(root, config_path),
        "data_source": "polymarket_public_api" if mode == FORMAL else "fixture" if source_endpoint == "fixture" or source_endpoint.startswith("fixture") else "polymarket_public_api",
        "run_environment": mode,
        "raw_response_hash": raw_hash,
    }


def raw_bytes_from_result(result: Any) -> bytes:
    raw = getattr(result, "raw_bytes", b"")
    if raw:
        return bytes(raw)
    return str(getattr(result, "raw_text", "") or "").encode("utf-8")


def canonical_json_from_bytes(raw_bytes: bytes) -> tuple[Any, str, str]:
    text = raw_bytes.decode("utf-8")
    payload = json.loads(text)
    canonical = stable_json(payload)
    return payload, canonical, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def record_http_evidence(
    conn: sqlite3.Connection,
    mode: str,
    evidence_type: str,
    run_id: str,
    signal_id: str,
    token_id: str,
    condition_id: str,
    market_slug: str,
    result: Any,
) -> str:
    raw_bytes = raw_bytes_from_result(result)
    raw_sha = hashlib.sha256(raw_bytes).hexdigest()
    try:
        payload, canonical_json, canonical_sha = canonical_json_from_bytes(raw_bytes)
        decoded = raw_bytes.decode("utf-8")
    except Exception:
        payload = None
        canonical_json = ""
        canonical_sha = ""
        decoded = raw_bytes.decode("utf-8", "replace")
    url = str(getattr(result, "url", "fixture") or "fixture")
    parsed = urllib.parse.urlparse(url)
    query_params = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    endpoint = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", "")) if parsed.scheme else url
    evidence_id = id_for(
        "ev",
        {
            "type": evidence_type,
            "run_id": run_id,
            "signal_id": signal_id,
            "token_id": token_id,
            "sha": raw_sha,
            "endpoint": endpoint,
        },
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO http_evidence(evidence_id,evidence_type,run_id,signal_id,token_id,condition_id,market_slug,endpoint,query_params_json,request_started_at_utc,response_received_at_utc,server_time_utc,http_status_code,response_content_type,raw_http_bytes,raw_http_sha256,decoded_text,canonical_json,canonical_json_sha256,adapter_name,adapter_version,mode)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            evidence_id,
            evidence_type,
            run_id,
            signal_id,
            token_id,
            condition_id,
            market_slug,
            endpoint,
            stable_json(query_params),
            str(getattr(result, "started_at_utc", "") or ""),
            str(getattr(result, "received_at_utc", "") or ""),
            "",
            int(getattr(result, "status_code", 200) or 200),
            str(getattr(result, "content_type", "application/json") or ""),
            raw_bytes,
            raw_sha,
            decoded,
            canonical_json,
            canonical_sha,
            ADAPTER_NAME,
            ADAPTER_VERSION,
            mode,
        ),
    )
    if payload is None:
        append_audit(conn, mode, run_id, "http_evidence_parse_failed", {"evidence_id": evidence_id, "type": evidence_type}, "warning")
    return evidence_id


def evidence_payload(conn: sqlite3.Connection, evidence_id: str, checks: dict[str, Any] | None = None, prefix: str = "MARKET") -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM http_evidence WHERE evidence_id=?", (evidence_id,)).fetchone()
    if row is None:
        if checks is not None:
            inc(checks, f"{prefix}_EVIDENCE_MISSING")
        return None
    raw_bytes = bytes(row["raw_http_bytes"])
    raw_sha = hashlib.sha256(raw_bytes).hexdigest()
    if raw_sha != row["raw_http_sha256"] and checks is not None:
        inc(checks, f"{prefix}_RAW_HTTP_HASH_MISMATCH")
    try:
        payload, canonical, canonical_sha = canonical_json_from_bytes(raw_bytes)
    except Exception:
        if checks is not None:
            inc(checks, f"{prefix}_RESPONSE_PARSE_FAILED")
        return None
    if canonical_sha != row["canonical_json_sha256"] and checks is not None:
        inc(checks, f"{prefix}_CANONICAL_HASH_MISMATCH")
    if row["canonical_json"] and stable_json(json.loads(row["canonical_json"])) != canonical and checks is not None:
        inc(checks, f"{prefix}_CANONICAL_HASH_MISMATCH")
    return payload


def build_public_adapter(config: dict[str, Any], mode: str = FORMAL) -> PublicAdapter:
    allowed_gamma = str(config["public_api"].get("gamma_base", GAMMA_BASE)).rstrip("/")
    allowed_clob = str(config["public_api"].get("clob_base", CLOB_BASE)).rstrip("/")
    if mode == FORMAL:
        if allowed_gamma != GAMMA_BASE or allowed_clob != CLOB_BASE:
            raise RuntimeError("formal adapter endpoints must match the frozen Polymarket public API allowlist")
        if config["public_api"].get("methods_allowed") and "GET" not in config["public_api"].get("methods_allowed", []):
            raise RuntimeError("formal adapter must allow public GET only")
    return PublicAdapter(allowed_gamma, allowed_clob, config["public_api"].get("timeout_seconds", 10), int(config["public_api"].get("max_retries", 2)), config["public_api"].get("backoff_seconds", Decimal("0.5")))


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


def create_run(conn: sqlite3.Connection, mode: str, command: str, config_hash: str, code_hashes: dict[str, str], run_id: str | None = None, now: datetime | None = None, lock_id: str = "") -> str:
    rid = run_id or make_run_id(command, now)
    conn.execute(
        "INSERT OR IGNORE INTO runs(run_id,mode,command,lock_id,started_at_utc,selected_tokens_json,snapshot_count,error_count,code_hash_json,config_hash,manifest_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (rid, mode, command, lock_id, now_iso(now), "[]", 0, 0, stable_json(code_hashes), config_hash, "{}"),
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
    expected_keys = set(json.loads(get_state(conn, "expected_frozen_file_keys", "[]") or "[]"))
    current_records = frozen_file_records(root, config_path)
    current_keys = set(current_records)
    drift: dict[str, Any] = {}
    if expected_keys != current_keys:
        drift["key_set_mismatch"] = {"expected": sorted(expected_keys), "current": sorted(current_keys)}
    for key in expected_keys:
        rec = current_records.get(key, {"missing": True})
        if rec.get("missing"):
            drift[key] = {"expected": get_state(conn, key, ""), "current": None, "reason": "missing_file"}
        elif get_state(conn, key, "") != rec.get("sha256"):
            drift[key] = {"expected": get_state(conn, key, ""), "current": rec.get("sha256"), "reason": "hash_changed"}
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
        records = frozen_file_records(root, config_path)
        missing = {k: v for k, v in records.items() if v["missing"]}
        if missing:
            raise RuntimeError(f"cannot start formal with missing frozen files: {sorted(missing)}")
        hashes = {k: str(v["sha256"]) for k, v in records.items()}
        with conn:
            set_state(conn, "formal_started_at_utc", now_iso(now))
            set_state(conn, "expected_frozen_file_keys", stable_json(sorted(records)))
            set_state(conn, "frozen_file_manifest", stable_json({"generated_at_utc": now_iso(now), "schema_version": SCHEMA_VERSION, "files": records}))
            for key, value in hashes.items():
                set_state(conn, key, value)
            append_audit(conn, FORMAL, "", "formal_started_v5_1_8", {"hashes": hashes}, "info", now)
        return {"status": "started", "formal_started_at_utc": now_iso(now)}
    finally:
        conn.close()


def signal_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {k: str(row.get(k, "")) for k in USER_SIGNAL_FIELDS}


def canonical_signal_from_payload(payload: dict[str, Any]) -> dict[str, str]:
    bucket_info = parse_temperature_bucket_info(str(payload.get("temperature_bucket") or ""))
    bucket_type = str(bucket_info.get("bucket_type") or payload.get("bucket_type") or "")
    threshold = "" if bucket_info.get("threshold_value") is None else dstr(bucket_info["threshold_value"])
    unit = str(bucket_info.get("unit") or payload.get("temperature_unit") or "").upper()
    canonical = {
        "signal_id": str(payload.get("signal_id", "")),
        "created_at_utc": str(payload.get("created_at_utc", "")),
        "city": str(payload.get("city", "")),
        "weather_date_local": str(payload.get("weather_date_local", "")),
        "weather_metric": normalize_metric(str(payload.get("weather_metric", ""))),
        "bucket_type": bucket_type,
        "temperature_threshold": threshold or str(payload.get("temperature_threshold", "")),
        "temperature_unit": unit,
        "market_slug": str(payload.get("market_slug", "")),
        "condition_id": str(payload.get("condition_id", "")),
        "token_id": str(payload.get("token_id", "")),
        "outcome": str(payload.get("outcome", "")),
        "side": str(payload.get("side", "")).upper(),
        "intended_usd": dstr(payload.get("intended_usd", "0")),
        "max_entry_price": dstr(payload.get("max_entry_price", "0")),
        "forecast_probability": "" if payload.get("forecast_probability") in (None, "") else dstr(payload.get("forecast_probability")),
        "market_probability_at_signal": "" if payload.get("market_probability_at_signal") in (None, "") else dstr(payload.get("market_probability_at_signal")),
        "source": str(payload.get("source", "")),
        "notes": str(payload.get("notes", "") or ""),
        "entry_deadline_utc": str(payload.get("entry_deadline_utc", "")),
    }
    return {k: canonical[k] for k in CANONICAL_SIGNAL_FIELDS}


def signal_evidence_from_row(root: Path, config_path: Path, row: dict[str, Any], payload: dict[str, Any], mode: str, now: datetime, run_id: str = "", lock_id: str = "") -> dict[str, Any]:
    raw_payload = {k: str(row.get(k, "")) for k in sorted(row)}
    raw_bytes = stable_json(raw_payload).encode("utf-8")
    raw_sha = hashlib.sha256(raw_bytes).hexdigest()
    canonical = canonical_signal_from_payload(payload)
    canonical_json = stable_json(canonical)
    canonical_sha = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    evidence_id = id_for("sig_ev", {"signal_id": payload["signal_id"], "mode": mode, "raw": raw_sha, "canonical": canonical_sha})
    hashes = current_hashes(root, config_path)
    return {
        "evidence_id": evidence_id,
        "raw_bytes": raw_bytes,
        "raw_sha": raw_sha,
        "canonical": canonical,
        "canonical_json": canonical_json,
        "canonical_sha": canonical_sha,
        "normalized_json": stable_json(payload),
        "registration_run_id": run_id,
        "registration_lock_id": lock_id,
        "registration_code_hash": hashes.get("core_code_sha256", ""),
        "registration_config_hash": hashes.get("config_sha256", ""),
    }


def temperature_bucket_from_signal(row: dict[str, Any]) -> str:
    explicit = str(row.get("temperature_bucket", "") or "").strip()
    if explicit:
        return explicit
    bucket_type = str(row.get("bucket_type", "") or "").strip().lower()
    threshold = str(row.get("temperature_threshold", "") or "").strip()
    unit = str(row.get("temperature_unit", "") or "").strip().upper()
    if not (bucket_type and threshold and unit):
        return ""
    canonical_type = {"exact": "exact", "or_below": "or_below", "below": "or_below", "or_higher": "or_higher", "higher": "or_higher"}.get(bucket_type)
    if canonical_type is None:
        return ""
    suffix = "" if canonical_type == "exact" else (" or below" if canonical_type == "or_below" else " or higher")
    return f"{threshold}{unit}{suffix}"


def validate_signal(row: dict[str, Any], mode: str, conn: sqlite3.Connection, config: dict[str, Any], now: datetime) -> dict[str, Any]:
    if row.get("registered_at_utc"):
        raise ValueError("user-supplied registered_at_utc is forbidden")
    created = parse_strict_utc_literal(str(row.get("created_at_utc", "")))
    delay = (now - created).total_seconds()
    max_delay = int(config["sample_rules"].get("max_signal_registration_delay_seconds", 300))
    future = int(config["sample_rules"].get("allowed_future_skew_seconds", 30))
    if delay > max_delay:
        raise ValueError("signal registration delay exceeded")
    if -delay > future:
        raise ValueError("signal timestamp is too far in the future")
    if mode == FORMAL:
        started = get_state(conn, "formal_started_at_utc", "")
        if not started:
            raise ValueError("formal sample is not started")
        if created < parse_utc(started) - timedelta(microseconds=1):
            raise ValueError("signal before formal start")
    if str(row.get("side", "")).upper() != "BUY":
        raise ValueError("only BUY entry signals are supported")
    for key in ["signal_id", "city", "weather_date_local", "weather_metric", "market_slug", "condition_id", "token_id", "outcome"]:
        if not row.get(key):
            raise ValueError(f"{key} is required")
    raw_bucket = temperature_bucket_from_signal(row)
    canonical_bucket = parse_temperature_bucket(raw_bucket)
    if not canonical_bucket:
        raise ValueError("temperature bucket is invalid")
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
        "temperature_bucket": canonical_bucket,
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
                    ev = signal_evidence_from_row(root, config_path, row, payload, mode, now)
                    sig_hash = ev["canonical_sha"]
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
                        INSERT INTO signal_registration_evidence(evidence_id,signal_id,original_signal_payload_bytes,original_signal_payload_sha256,canonical_signal_json,canonical_signal_sha256,normalized_signal_fields_json,registered_at_utc,registration_run_id,registration_lock_id,registration_code_hash,registration_config_hash,mode)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            ev["evidence_id"],
                            sid,
                            ev["raw_bytes"],
                            ev["raw_sha"],
                            ev["canonical_json"],
                            ev["canonical_sha"],
                            ev["normalized_json"],
                            now.isoformat(),
                            ev["registration_run_id"],
                            ev["registration_lock_id"],
                            ev["registration_code_hash"],
                            ev["registration_config_hash"],
                            mode,
                        ),
                    )
                    conn.execute(
                        """
                        INSERT INTO signals(signal_id,signal_hash,registration_evidence_id,original_signal_payload_sha256,canonical_signal_sha256,registration_run_id,registration_lock_id,registration_code_hash,registration_config_hash,registration_audit_id,created_at_utc,registered_at_utc,city,city_normalized,weather_date_local,weather_metric,temperature_bucket,event_key,market_slug,condition_id,token_id,outcome,side,forecast_temperature,forecast_probability,market_probability_at_signal,intended_usd,max_entry_price,entry_deadline_utc,source,notes,mode)
                        VALUES(:signal_id,:signal_hash,:registration_evidence_id,:original_signal_payload_sha256,:canonical_signal_sha256,:registration_run_id,:registration_lock_id,:registration_code_hash,:registration_config_hash,:registration_audit_id,:created_at_utc,:registered_at_utc,:city,:city_normalized,:weather_date_local,:weather_metric,:temperature_bucket,:event_key,:market_slug,:condition_id,:token_id,:outcome,:side,:forecast_temperature,:forecast_probability,:market_probability_at_signal,:intended_usd,:max_entry_price,:entry_deadline_utc,:source,:notes,:mode)
                        """,
                        {
                            **payload,
                            "signal_hash": sig_hash,
                            "registration_evidence_id": ev["evidence_id"],
                            "original_signal_payload_sha256": ev["raw_sha"],
                            "canonical_signal_sha256": ev["canonical_sha"],
                            "registration_run_id": ev["registration_run_id"],
                            "registration_lock_id": ev["registration_lock_id"],
                            "registration_code_hash": ev["registration_code_hash"],
                            "registration_config_hash": ev["registration_config_hash"],
                            "registration_audit_id": audit_id,
                        },
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


def rebuild_entry_state(conn: sqlite3.Connection, sig_or_signal_id: sqlite3.Row | str, mode: str, now: datetime | None = None) -> dict[str, Any]:
    sig = get_signal(conn, sig_or_signal_id, mode) if isinstance(sig_or_signal_id, str) else sig_or_signal_id
    filled_usd = dec(conn.execute("SELECT COALESCE(SUM(CAST(gross_entry_cost AS REAL)),0) v FROM entry_fills WHERE signal_id=? AND mode=?", (sig["signal_id"], mode)).fetchone()["v"])
    filled_shares = dec(conn.execute("SELECT COALESCE(SUM(CAST(filled_shares AS REAL)),0) v FROM entry_fills WHERE signal_id=? AND mode=?", (sig["signal_id"], mode)).fetchone()["v"])
    intended = dec(sig["intended_usd"])
    remaining = max(intended - filled_usd, ZERO)
    deadline = parse_utc(sig["entry_deadline_utc"])
    if filled_usd >= intended - EPS:
        status_value = "filled"
    elif now and now.astimezone(timezone.utc) > deadline:
        status_value = "expired"
    elif filled_usd > EPS:
        status_value = "partial"
    else:
        status_value = "pending"
    last = conn.execute("SELECT filled_at_utc FROM entry_fills WHERE signal_id=? AND mode=? ORDER BY filled_at_utc DESC,row_id DESC LIMIT 1", (sig["signal_id"], mode)).fetchone()
    return {
        "signal_id": sig["signal_id"],
        "token_id": sig["token_id"],
        "intended_usd": dstr(intended),
        "filled_entry_usd": dstr(filled_usd),
        "remaining_entry_usd": dstr(remaining),
        "filled_entry_shares": dstr(filled_shares),
        "entry_status": status_value,
        "max_entry_price": sig["max_entry_price"],
        "entry_deadline_utc": sig["entry_deadline_utc"],
        "last_fill_at": last["filled_at_utc"] if last else "",
    }


ENTRY_STATE_CODES = [
    "ENTRY_STATE_FILLED_USD_MISMATCH",
    "ENTRY_STATE_REMAINING_USD_MISMATCH",
    "ENTRY_STATE_SHARES_MISMATCH",
    "ENTRY_STATE_STATUS_MISMATCH",
    "ENTRY_STATE_DEADLINE_MISMATCH",
    "ENTRY_STATE_REOPENED_AFTER_FILLED",
    "ENTRY_STATE_REOPENED_AFTER_EXPIRED",
    "ENTRY_STATE_CACHE_CORRUPTED",
]


def entry_state_cache_errors(conn: sqlite3.Connection, sig: sqlite3.Row, mode: str, now: datetime | None = None) -> list[str]:
    state = latest_entry_state(conn, sig["signal_id"])
    derived = rebuild_entry_state(conn, sig, mode, now)
    if not state:
        return ["ENTRY_STATE_CACHE_CORRUPTED"]
    errors: list[str] = []
    if not same_decimal(state["filled_entry_usd"], derived["filled_entry_usd"]):
        errors.append("ENTRY_STATE_FILLED_USD_MISMATCH")
    if not same_decimal(state["remaining_entry_usd"], derived["remaining_entry_usd"]):
        errors.append("ENTRY_STATE_REMAINING_USD_MISMATCH")
    if not same_decimal(state["filled_entry_shares"], derived["filled_entry_shares"]):
        errors.append("ENTRY_STATE_SHARES_MISMATCH")
    if str(state["entry_deadline_utc"]) != str(derived["entry_deadline_utc"]):
        errors.append("ENTRY_STATE_DEADLINE_MISMATCH")
    if str(state["entry_status"]) != str(derived["entry_status"]):
        errors.append("ENTRY_STATE_STATUS_MISMATCH")
        if derived["entry_status"] == "filled" and state["entry_status"] in {"pending", "partial"}:
            errors.append("ENTRY_STATE_REOPENED_AFTER_FILLED")
        if derived["entry_status"] == "expired" and state["entry_status"] in {"pending", "partial"}:
            errors.append("ENTRY_STATE_REOPENED_AFTER_EXPIRED")
    return errors


def state_preflight(conn: sqlite3.Connection, mode: str, sig: sqlite3.Row, run_id: str, now: datetime | None = None) -> dict[str, Any]:
    errors = entry_state_cache_errors(conn, sig, mode, now)
    missing_evidence = not bool(sig["registration_evidence_id"]) or conn.execute("SELECT 1 FROM signal_registration_evidence WHERE evidence_id=? AND mode=?", (sig["registration_evidence_id"], mode)).fetchone() is None
    if missing_evidence:
        errors.append("SIGNAL_REGISTRATION_EVIDENCE_MISSING")
    if errors:
        append_audit(conn, mode, run_id, "state_preflight_failed", {"signal_id": sig["signal_id"], "errors": sorted(set(errors))}, "error", now)
        return {"ok": False, "signal_id": sig["signal_id"], "errors": sorted(set(errors))}
    return {"ok": True, "signal_id": sig["signal_id"], "derived": rebuild_entry_state(conn, sig, mode, now)}


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
    return sorted({r["signal_id"] for r in rows if not signal_is_complete(conn, r["signal_id"], mode)})


def signal_is_complete(conn: sqlite3.Connection, signal_id: str, mode: str) -> bool:
    state = latest_entry_state(conn, signal_id)
    if state and state["entry_status"] in {"pending", "partial"}:
        return False
    for strategy_id in STRATEGY_IDS:
        if not is_settled(conn, signal_id, strategy_id, mode):
            return False
        latest = conn.execute(
            "SELECT * FROM strategy_triggers WHERE signal_id=? AND strategy_id=? AND mode=? ORDER BY row_id DESC LIMIT 1",
            (signal_id, strategy_id, mode),
        ).fetchone()
        if latest and latest["trigger_status"] == "open":
            return False
        open_shares = sum((lot["open_shares"] for lot in lot_open_rows(conn, signal_id, strategy_id, mode)), ZERO)
        if open_shares > EPS:
            return False
    return True


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


def record_snapshot(
    conn: sqlite3.Connection,
    run_id: str,
    mode: str,
    sig: sqlite3.Row,
    purpose: str,
    raw_book: dict[str, Any],
    source_endpoint: str,
    now: datetime | None = None,
    gamma_market: dict[str, Any] | None = None,
    root: Path = PROJECT_ROOT,
    config_path: Path | None = None,
    lock_id: str = "",
    orderbook_evidence_id: str = "",
    gamma_market_evidence_id: str = "",
    clob_market_evidence_id: str = "",
) -> tuple[str, dict[str, Any], bool]:
    book = normalize_orderbook(raw_book, sig["token_id"], sig["condition_id"], gamma_market)
    config_path = config_path or (root / "config/forward_simulation_v5_1_8.yaml")
    evidence_row = conn.execute("SELECT * FROM http_evidence WHERE evidence_id=?", (orderbook_evidence_id,)).fetchone() if orderbook_evidence_id else None
    if evidence_row:
        raw_response = str(evidence_row["decoded_text"] or "")
        raw_response_sha256 = str(evidence_row["raw_http_sha256"])
        canonical_json_sha256 = str(evidence_row["canonical_json_sha256"])
    else:
        raw_response = stable_json(raw_book)
        raw_response_sha256 = hashlib.sha256(raw_response.encode("utf-8")).hexdigest()
        canonical_json_sha256 = raw_response_sha256
    book["raw_response_sha256"] = raw_response_sha256
    market_constraints = {
        "condition_id": sig["condition_id"],
        "token_id": sig["token_id"],
        "market_slug": sig["market_slug"],
        "tick_size": book["tick_size"],
        "min_order_size": book["min_order_size"],
        "gamma_tick_size": book.get("gamma_tick_size"),
        "gamma_min_order_size": book.get("gamma_min_order_size"),
        "clob_tick_size": book.get("clob_tick_size"),
        "clob_min_order_size": book.get("clob_min_order_size"),
        "constraint_crosscheck_status": book.get("constraint_crosscheck_status"),
    }
    market_constraints_hash = content_hash(market_constraints)
    book["market_constraints_hash"] = market_constraints_hash
    meta = source_meta(root, config_path, mode, source_endpoint, raw_response_sha256)
    snapshot_id = id_for("ob", {"run_id": run_id, "token_id": sig["token_id"], "purpose": purpose, "content_hash": book["content_hash"]})
    exists = conn.execute("SELECT 1 FROM orderbook_snapshots WHERE run_id=? AND snapshot_id=?", (run_id, snapshot_id)).fetchone()
    if exists:
        return snapshot_id, book, False
    conn.execute(
        """
        INSERT INTO orderbook_snapshots(run_id,snapshot_id,orderbook_evidence_id,gamma_market_evidence_id,clob_market_evidence_id,lock_id,content_hash,captured_at_utc,token_id,condition_id,market_slug,purpose,best_bid,best_ask,spread,tick_size,min_order_size,neg_risk,raw_orderbook_json,raw_response,raw_response_sha256,raw_http_sha256,canonical_json_sha256,response_content_type,http_status_code,request_params_json,server_time_utc,normalized_bids_json,normalized_asks_json,normalized_book_json,normalized_book_sha256,normalization_algorithm_version,market_constraints_hash,bid_levels_count,ask_levels_count,total_bid_shares,total_ask_shares,source_endpoint,adapter_name,adapter_version,adapter_code_hash,data_source,run_environment,raw_response_hash,gamma_tick_size,gamma_min_order_size,clob_tick_size,clob_min_order_size,selected_tick_size,selected_min_order_size,constraint_crosscheck_status,constraint_conflict_details,mode)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            run_id,
            snapshot_id,
            orderbook_evidence_id,
            gamma_market_evidence_id,
            clob_market_evidence_id,
            lock_id,
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
            raw_response,
            raw_response_sha256,
            raw_response_sha256,
            canonical_json_sha256,
            "application/json",
            200,
            stable_json({"token_id": sig["token_id"]}),
            str(raw_book.get("timestamp") or ""),
            stable_json(book["bids"]),
            stable_json(book["asks"]),
            book["normalized_book_json"],
            book["normalized_book_sha256"],
            book["normalization_algorithm_version"],
            market_constraints_hash,
            int(book["bid_depth_levels"]),
            int(book["ask_depth_levels"]),
            dstr(book["total_bid_shares"]),
            dstr(book["total_ask_shares"]),
            source_endpoint,
            meta["adapter_name"],
            meta["adapter_version"],
            meta["adapter_code_hash"],
            meta["data_source"],
            meta["run_environment"],
            meta["raw_response_hash"],
            "" if book.get("gamma_tick_size") is None else dstr(book["gamma_tick_size"]),
            "" if book.get("gamma_min_order_size") is None else dstr(book["gamma_min_order_size"]),
            "" if book.get("clob_tick_size") is None else dstr(book["clob_tick_size"]),
            "" if book.get("clob_min_order_size") is None else dstr(book["clob_min_order_size"]),
            "" if book.get("selected_tick_size") is None else dstr(book["selected_tick_size"]),
            "" if book.get("selected_min_order_size") is None else dstr(book["selected_min_order_size"]),
            str(book.get("constraint_crosscheck_status") or ""),
            str(book.get("constraint_conflict_details") or ""),
            mode,
        ),
    )
    return snapshot_id, book, True


def snapshot_meta(conn: sqlite3.Connection, snapshot_id: str, mode: str) -> dict[str, str]:
    row = conn.execute("SELECT adapter_name,adapter_version,adapter_code_hash,data_source,run_environment,raw_response_hash,raw_response_sha256,normalized_book_sha256,tick_size,min_order_size,orderbook_evidence_id,gamma_market_evidence_id,clob_market_evidence_id FROM orderbook_snapshots WHERE snapshot_id=? AND mode=? ORDER BY row_id DESC LIMIT 1", (snapshot_id, mode)).fetchone()
    if not row:
        return {"adapter_name": "", "adapter_version": "", "adapter_code_hash": "", "data_source": "", "run_environment": mode, "raw_response_hash": "", "raw_response_sha256": "", "normalized_book_sha256": "", "tick_size": "", "min_order_size": "", "orderbook_evidence_id": "", "gamma_market_evidence_id": "", "clob_market_evidence_id": ""}
    return {k: row[k] for k in row.keys()}


def fill_replay_values(action: str, book: dict[str, Any], fill: dict[str, Any], fee_calc: dict[str, Any], fee_policy: dict[str, Any], requested: Decimal, status: str) -> dict[str, Any]:
    side = "ask" if action == "buy" else "bid"
    best_price = book["best_ask"] if action == "buy" else book["best_bid"]
    vwap = fill.get("vwap")
    if best_price is None or vwap is None:
        slippage = None
    elif action == "buy":
        slippage = dec(vwap) - dec(best_price)
    else:
        slippage = dec(best_price) - dec(vwap)
    unfilled = fill.get("remaining_usd") if action == "buy" else fill.get("remaining_shares")
    return {
        "fill_algorithm_version": FILL_ALGORITHM_VERSION,
        "action": action,
        "side": side,
        "requested_amount": dstr(requested),
        "consumed_levels_json": stable_json(fill.get("levels") or []),
        "normalized_book_sha256": str(book.get("normalized_book_sha256") or book.get("content_hash") or ""),
        "raw_response_sha256": str(book.get("raw_response_sha256") or ""),
        "filled_notional": dstr(fill["filled_usd"]),
        "best_price_at_snapshot": "" if best_price is None else dstr(best_price),
        "slippage_vs_best": "" if slippage is None else dstr(slippage),
        "levels_consumed_count": len(fill.get("levels") or []),
        "fully_filled": int(status == "filled" or dec(unfilled or "0") <= EPS),
        "unfilled_amount": dstr(unfilled or ZERO),
        "gross_notional": dstr(fee_calc["gross_notional"]),
        "official_fee": dstr(fee_calc["official_fee"]),
        "net_cost_or_proceeds": dstr(fee_calc["net_cost_or_proceeds"]),
        "tick_size": dstr(book["tick_size"]),
        "min_order_size": dstr(book["min_order_size"]),
        "fee_rate": "" if fee_policy.get("fee_rate") is None else dstr(fee_policy.get("fee_rate")),
        "fee_exponent": "" if fee_policy.get("clob_fee_exponent") is None else dstr(fee_policy.get("clob_fee_exponent")),
    }


def update_entry_fill_replay_fields(conn: sqlite3.Connection, fill_id: str, book: dict[str, Any], fill: dict[str, Any], fee_calc: dict[str, Any], fee_policy: dict[str, Any], requested_usd: Decimal, status: str, lot_ids: list[str]) -> None:
    values = fill_replay_values("buy", book, fill, fee_calc, fee_policy, requested_usd, status)
    conn.execute(
        """
        UPDATE entry_fills
        SET fill_algorithm_version=:fill_algorithm_version, action=:action, side=:side,
            requested_amount=:requested_amount, requested_usd=:requested_amount,
            consumed_levels_json=:consumed_levels_json, normalized_book_sha256=:normalized_book_sha256,
            raw_response_sha256=:raw_response_sha256, filled_notional=:filled_notional,
            best_price_at_snapshot=:best_price_at_snapshot, slippage_vs_best=:slippage_vs_best,
            levels_consumed_count=:levels_consumed_count, fully_filled=:fully_filled,
            unfilled_amount=:unfilled_amount, gross_notional=:gross_notional,
            official_fee=:official_fee, net_cost_or_proceeds=:net_cost_or_proceeds,
            tick_size=:tick_size, min_order_size=:min_order_size, fee_rate=:fee_rate,
            fee_exponent=:fee_exponent, position_lot_id=:position_lot_id
        WHERE entry_fill_id=:fill_id
        """,
        {**values, "position_lot_id": ",".join(lot_ids), "fill_id": fill_id},
    )


def update_exit_fill_replay_fields(conn: sqlite3.Connection, fill_id: str, book: dict[str, Any], fill: dict[str, Any], fee_calc: dict[str, Any], fee_policy: dict[str, Any], requested_shares: Decimal, status: str) -> None:
    values = fill_replay_values("sell", book, fill, fee_calc, fee_policy, requested_shares, status)
    conn.execute(
        """
        UPDATE exit_fills
        SET fill_algorithm_version=:fill_algorithm_version, action=:action, side=:side,
            requested_amount=:requested_amount, requested_shares=:requested_amount,
            consumed_levels_json=:consumed_levels_json, normalized_book_sha256=:normalized_book_sha256,
            raw_response_sha256=:raw_response_sha256, filled_notional=:filled_notional,
            best_price_at_snapshot=:best_price_at_snapshot, slippage_vs_best=:slippage_vs_best,
            levels_consumed_count=:levels_consumed_count, fully_filled=:fully_filled,
            unfilled_amount=:unfilled_amount, gross_notional=:gross_notional,
            official_fee=:official_fee, net_cost_or_proceeds=:net_cost_or_proceeds,
            tick_size=:tick_size, min_order_size=:min_order_size, fee_rate=:fee_rate,
            fee_exponent=:fee_exponent
        WHERE exit_fill_id=:fill_id
        """,
        {**values, "fill_id": fill_id},
    )


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

    def market_bundle_without_book(self, sig: sqlite3.Row) -> tuple[Any, Any]:
        gamma = self.adapter.market_by_slug(sig["market_slug"])
        clob = self.adapter.clob_market_info(sig["condition_id"])
        return gamma, clob

    def clob_public_market(self, condition_id: str) -> Any:
        return self.adapter.clob_public_market(condition_id)

    def orderbook(self, token_id: str) -> Any:
        return self.adapter.orderbook(token_id)


def consume_buy_levels(levels: list[dict[str, Decimal]], intended_usd: Decimal, max_price: Decimal, min_order_size: Decimal) -> dict[str, Any]:
    remaining = intended_usd
    shares = ZERO
    gross = ZERO
    used: list[dict[str, Decimal]] = []
    for level in levels:
        if remaining <= EPS or level["price"] > max_price:
            break
        before = level["size"]
        qty = min(level["size"], remaining / level["price"]) if level["price"] > ZERO else ZERO
        if qty <= EPS:
            continue
        usd = qty * level["price"]
        level["size"] -= qty
        remaining -= usd
        shares += qty
        gross += usd
        used.append({
            "price": level["price"],
            "shares": qty,
            "usd": usd,
            "book_price": level["price"],
            "available_shares_before": before,
            "consumed_shares": qty,
            "available_shares_after": level["size"],
            "notional": usd,
            "sequence_index": len(used) + 1,
        })
    if ZERO < shares < min_order_size:
        for used_level in used:
            for level in levels:
                if level["price"] == used_level["price"]:
                    level["size"] += used_level["shares"]
                    break
        return {"status": "below_min_order_size", "filled_shares": ZERO, "filled_usd": ZERO, "remaining_usd": intended_usd, "vwap": None, "levels": []}
    return {"status": "filled" if remaining <= EPS else "partial", "filled_shares": shares, "filled_usd": gross, "remaining_usd": max(remaining, ZERO), "vwap": gross / shares if shares > ZERO else None, "levels": used}


def consume_sell_levels(levels: list[dict[str, Decimal]], shares_to_sell: Decimal, min_order_size: Decimal, mutate: bool = True) -> dict[str, Any]:
    if shares_to_sell < min_order_size:
        return {"status": "below_min_order_size", "filled_shares": ZERO, "filled_usd": ZERO, "remaining_shares": shares_to_sell, "vwap": None, "levels": []}
    remaining = shares_to_sell
    shares = ZERO
    gross = ZERO
    used: list[dict[str, Decimal]] = []
    for level in levels:
        if remaining <= EPS:
            break
        before = level["size"]
        qty = min(level["size"], remaining)
        if qty <= EPS:
            continue
        usd = qty * level["price"]
        if mutate:
            level["size"] -= qty
        remaining -= qty
        shares += qty
        gross += usd
        used.append({
            "price": level["price"],
            "shares": qty,
            "usd": usd,
            "book_price": level["price"],
            "available_shares_before": before,
            "consumed_shares": qty,
            "available_shares_after": level["size"] if mutate else before - qty,
            "notional": usd,
            "sequence_index": len(used) + 1,
        })
    if ZERO < shares < min_order_size:
        if mutate:
            for used_level in used:
                for level in levels:
                    if level["price"] == used_level["price"]:
                        level["size"] += used_level["shares"]
                        break
        return {"status": "below_min_order_size", "filled_shares": ZERO, "filled_usd": ZERO, "remaining_shares": shares_to_sell, "vwap": None, "levels": []}
    return {"status": "filled" if remaining <= EPS else "partial", "filled_shares": shares, "filled_usd": gross, "remaining_shares": max(remaining, ZERO), "vwap": gross / shares if shares > ZERO else None, "levels": used, "remaining_below_min_order_size": ZERO < remaining < min_order_size}


def process_entry_with_bundle(conn: sqlite3.Connection, run_id: str, mode: str, sig: sqlite3.Row, bundle: MarketBundle, now: datetime | None = None, lock_id: str = "") -> dict[str, Any]:
    preflight = state_preflight(conn, mode, sig, run_id, now)
    if not preflight["ok"]:
        return {"signal_id": sig["signal_id"], "status": "state_preflight_failed", "errors": preflight["errors"]}
    state = preflight["derived"]
    if state["entry_status"] not in {"pending", "partial"}:
        return {"signal_id": sig["signal_id"], "status": "entry_not_active"}
    if now and now > parse_utc(sig["entry_deadline_utc"]):
        conn.execute(
            "INSERT INTO entry_order_state(signal_id,token_id,updated_at_utc,intended_usd,filled_entry_usd,remaining_entry_usd,filled_entry_shares,entry_status,max_entry_price,entry_deadline_utc,last_entry_attempt_at,last_attempt_reason,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sig["signal_id"], sig["token_id"], now_iso(now), sig["intended_usd"], state["filled_entry_usd"], state["remaining_entry_usd"], state["filled_entry_shares"], "expired", sig["max_entry_price"], sig["entry_deadline_utc"], now_iso(now), "entry_deadline_reached", mode),
        )
        return {"signal_id": sig["signal_id"], "status": "expired"}
    gamma_eid = record_http_evidence(conn, mode, "gamma_market", run_id, sig["signal_id"], sig["token_id"], sig["condition_id"], sig["market_slug"], bundle.gamma_result)
    clob_eid = record_http_evidence(conn, mode, "clob_market", run_id, sig["signal_id"], sig["token_id"], sig["condition_id"], sig["market_slug"], bundle.clob_result)
    ob_eid = record_http_evidence(conn, mode, "orderbook", run_id, sig["signal_id"], sig["token_id"], sig["condition_id"], sig["market_slug"], bundle.book_result)
    fee_policy = extract_fee_policy(bundle.gamma_market, bundle.clob_info)
    insert_fee_validation(conn, run_id, mode, sig, fee_policy)
    if fee_policy["fee_crosscheck_status"] in {"conflict", "unknown"}:
        append_audit(conn, mode, run_id, "entry_fee_policy_rejected", {"signal_id": sig["signal_id"], "status": fee_policy["fee_crosscheck_status"], "details": fee_policy["fee_conflict_details"]}, "warning", now)
        return {"signal_id": sig["signal_id"], "status": "fee_policy_rejected"}
    snapshot_id, book, inserted = record_snapshot(conn, run_id, mode, sig, "entry", bundle.raw_book, bundle.book_result.url, now, bundle.gamma_market, lock_id=lock_id, orderbook_evidence_id=ob_eid, gamma_market_evidence_id=gamma_eid, clob_market_evidence_id=clob_eid)
    validation = validate_token_mapping(dict(sig), bundle.gamma_market, bundle.clob_info, book)
    insert_token_validation(conn, run_id, mode, sig, validation)
    if not validation["mapping_valid"]:
        append_audit(conn, mode, run_id, "token_mapping_rejected", {"signal_id": sig["signal_id"], "errors": validation["errors"]}, "warning", now)
        return {"signal_id": sig["signal_id"], "status": "mapping_rejected"}
    duplicate = conn.execute("SELECT 1 FROM entry_fills WHERE run_id=? AND signal_id=? AND snapshot_id=?", (run_id, sig["signal_id"], snapshot_id)).fetchone()
    if duplicate or not inserted:
        return {"signal_id": sig["signal_id"], "status": "skipped_duplicate_snapshot"}
    requested_usd = dec(state["remaining_entry_usd"])
    buy = consume_buy_depth(book, requested_usd, dec(sig["max_entry_price"]))
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
    sm = snapshot_meta(conn, snapshot_id, mode)
    conn.execute(
        "INSERT INTO entry_fills(entry_fill_id,run_id,lock_id,signal_id,signal_registration_evidence_id,gamma_market_evidence_id,clob_market_evidence_id,orderbook_evidence_id,event_key,token_id,snapshot_id,filled_at_utc,gross_entry_cost,entry_fee,net_entry_cost,filled_shares,entry_vwap,fee_status,best_bid,best_ask,spread,complete_fill,unfilled_usd_after_fill,depth_levels_json,adapter_name,adapter_version,adapter_code_hash,data_source,run_environment,raw_response_hash,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (fill_id, run_id, lock_id, sig["signal_id"], sig["registration_evidence_id"], gamma_eid, clob_eid, ob_eid, sig["event_key"], sig["token_id"], snapshot_id, now_iso(now), dstr(fee_calc["gross_notional"]), dstr(fee_calc["official_fee"]), dstr(fee_calc["net_cost_or_proceeds"]), dstr(buy["filled_shares"]), dstr(buy["vwap"]), fee_calc["fee_status"], None if book["best_bid"] is None else dstr(book["best_bid"]), None if book["best_ask"] is None else dstr(book["best_ask"]), None if book["spread"] is None else dstr(book["spread"]), int(status == "filled"), dstr(max(dec(sig["intended_usd"]) - new_filled_usd, ZERO)), stable_json(buy["levels"]), sm["adapter_name"], sm["adapter_version"], sm["adapter_code_hash"], sm["data_source"], sm["run_environment"], sm["raw_response_hash"], mode),
    )
    lot_ids: list[str] = []
    for strategy_id in STRATEGY_IDS:
        lot_id = id_for("lot", {"strategy_id": strategy_id, "entry_fill_id": fill_id})
        lot_ids.append(lot_id)
        conn.execute(
            "INSERT INTO strategy_lots(lot_id,run_id,strategy_id,signal_id,event_key,token_id,entry_fill_id,created_at_utc,entry_shares,gross_entry_cost,entry_fee,net_entry_cost,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (lot_id, run_id, strategy_id, sig["signal_id"], sig["event_key"], sig["token_id"], fill_id, now_iso(now), dstr(buy["filled_shares"]), dstr(fee_calc["gross_notional"]), dstr(fee_calc["official_fee"]), dstr(fee_calc["net_cost_or_proceeds"]), mode),
        )
    update_entry_fill_replay_fields(conn, fill_id, book, buy, fee_calc, fee_policy, requested_usd, status, lot_ids)
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


def process_exit_with_bundle(conn: sqlite3.Connection, run_id: str, mode: str, sig: sqlite3.Row, bundle: MarketBundle, now: datetime | None = None, lock_id: str = "") -> list[dict[str, Any]]:
    if all(is_settled(conn, sig["signal_id"], st, mode) for st in STRATEGY_IDS):
        return [{"signal_id": sig["signal_id"], "status": "already_settled"}]
    gamma_eid = record_http_evidence(conn, mode, "gamma_market", run_id, sig["signal_id"], sig["token_id"], sig["condition_id"], sig["market_slug"], bundle.gamma_result)
    clob_eid = record_http_evidence(conn, mode, "clob_market", run_id, sig["signal_id"], sig["token_id"], sig["condition_id"], sig["market_slug"], bundle.clob_result)
    ob_eid = record_http_evidence(conn, mode, "orderbook", run_id, sig["signal_id"], sig["token_id"], sig["condition_id"], sig["market_slug"], bundle.book_result)
    fee_policy = extract_fee_policy(bundle.gamma_market, bundle.clob_info)
    if fee_policy["fee_crosscheck_status"] in {"conflict", "unknown"}:
        append_audit(conn, mode, run_id, "exit_fee_policy_rejected", {"signal_id": sig["signal_id"], "status": fee_policy["fee_crosscheck_status"]}, "warning", now)
        return [{"signal_id": sig["signal_id"], "status": "fee_policy_rejected"}]
    snapshot_id, book, inserted = record_snapshot(conn, run_id, mode, sig, "exit", bundle.raw_book, bundle.book_result.url, now, bundle.gamma_market, lock_id=lock_id, orderbook_evidence_id=ob_eid, gamma_market_evidence_id=gamma_eid, clob_market_evidence_id=clob_eid)
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
        sm = snapshot_meta(conn, snapshot_id, mode)
        conn.execute(
            "INSERT INTO exit_fills(exit_fill_id,run_id,lock_id,trigger_id,signal_id,strategy_id,signal_registration_evidence_id,gamma_market_evidence_id,clob_market_evidence_id,orderbook_evidence_id,trigger_stage_id,event_key,token_id,snapshot_id,filled_at_utc,planned_sell_shares,filled_shares,gross_exit_proceeds,exit_fee,net_exit_proceeds,exit_vwap,fee_status,best_bid,best_ask,spread,complete_fill,unfilled_trigger_shares_after_fill,depth_levels_json,adapter_name,adapter_version,adapter_code_hash,data_source,run_environment,raw_response_hash,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (fill_id, run_id, lock_id, trigger["trigger_id"], sig["signal_id"], strategy_id, sig["registration_evidence_id"], gamma_eid, clob_eid, ob_eid, trigger["trigger_stage_id"], sig["event_key"], sig["token_id"], snapshot_id, now_iso(now), dstr(planned), dstr(sell["filled_shares"]), dstr(fee_calc["gross_notional"]), dstr(fee_calc["official_fee"]), dstr(fee_calc["net_cost_or_proceeds"]), dstr(sell["vwap"]), fee_calc["fee_status"], None if book["best_bid"] is None else dstr(book["best_bid"]), None if book["best_ask"] is None else dstr(book["best_ask"]), None if book["spread"] is None else dstr(book["spread"]), int(sell["remaining_shares"] <= EPS), dstr(max(planned - sell["filled_shares"], ZERO)), stable_json(sell["levels"]), sm["adapter_name"], sm["adapter_version"], sm["adapter_code_hash"], sm["data_source"], sm["run_environment"], sm["raw_response_hash"], mode),
        )
        allocate_fifo(conn, run_id, mode, sig["signal_id"], strategy_id, sell["filled_shares"], fee_calc["gross_notional"], fee_calc["official_fee"], fee_calc["net_cost_or_proceeds"], fill_id, trigger["trigger_id"])
        update_trigger(conn, trigger, sell["filled_shares"], now)
        update_exit_fill_replay_fields(conn, fill_id, book, sell, fee_calc, fee_policy, planned, "filled" if sell["remaining_shares"] <= EPS else "partial")
        results.append({"signal_id": sig["signal_id"], "strategy_id": strategy_id, "status": "exit_filled", "filled_shares": dstr(sell["filled_shares"])})
    return results


def process_active_token_batch(
    conn: sqlite3.Connection,
    run_id: str,
    mode: str,
    token_id: str,
    signal_bundles: list[tuple[sqlite3.Row, Any, Any]],
    book_result: Any,
    now: datetime | None = None,
    root: Path = PROJECT_ROOT,
    config_path: Path | None = None,
    lock_id: str = "",
) -> list[dict[str, Any]]:
    """Process one token with one shared orderbook snapshot for this round."""
    results: list[dict[str, Any]] = []
    if not signal_bundles:
        return results
    first_sig, first_gamma, first_clob = signal_bundles[0]
    first_gamma_eid = record_http_evidence(conn, mode, "gamma_market", run_id, first_sig["signal_id"], first_sig["token_id"], first_sig["condition_id"], first_sig["market_slug"], first_gamma)
    first_clob_eid = record_http_evidence(conn, mode, "clob_market", run_id, first_sig["signal_id"], first_sig["token_id"], first_sig["condition_id"], first_sig["market_slug"], first_clob)
    ob_eid = record_http_evidence(conn, mode, "orderbook", run_id, first_sig["signal_id"], first_sig["token_id"], first_sig["condition_id"], first_sig["market_slug"], book_result)
    snapshot_id, book, inserted = record_snapshot(conn, run_id, mode, first_sig, "monitor", book_result.payload, book_result.url, now, first_gamma.payload, root, config_path, lock_id, ob_eid, first_gamma_eid, first_clob_eid)
    if not inserted:
        return [{"token_id": token_id, "status": "skipped_duplicate_snapshot", "snapshot_id": snapshot_id}]

    ask_levels = [dict(level) for level in book["asks"]]
    ordered_for_entry = sorted(signal_bundles, key=lambda item: (item[0]["created_at_utc"], item[0]["signal_id"]))
    for sig, gamma_result, clob_result in ordered_for_entry:
        gamma_eid = record_http_evidence(conn, mode, "gamma_market", run_id, sig["signal_id"], sig["token_id"], sig["condition_id"], sig["market_slug"], gamma_result)
        clob_eid = record_http_evidence(conn, mode, "clob_market", run_id, sig["signal_id"], sig["token_id"], sig["condition_id"], sig["market_slug"], clob_result)
        preflight = state_preflight(conn, mode, sig, run_id, now)
        if not preflight["ok"]:
            results.append({"signal_id": sig["signal_id"], "status": "state_preflight_failed", "errors": preflight["errors"]})
            continue
        state = preflight["derived"]
        if state["entry_status"] not in {"pending", "partial"}:
            continue
        if now and now > parse_utc(sig["entry_deadline_utc"]):
            conn.execute(
                "INSERT INTO entry_order_state(signal_id,token_id,updated_at_utc,intended_usd,filled_entry_usd,remaining_entry_usd,filled_entry_shares,entry_status,max_entry_price,entry_deadline_utc,last_entry_attempt_at,last_attempt_reason,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sig["signal_id"], sig["token_id"], now_iso(now), sig["intended_usd"], state["filled_entry_usd"], state["remaining_entry_usd"], state["filled_entry_shares"], "expired", sig["max_entry_price"], sig["entry_deadline_utc"], now_iso(now), "entry_deadline_reached", mode),
            )
            results.append({"signal_id": sig["signal_id"], "status": "expired"})
            continue
        fee_policy = extract_fee_policy(gamma_result.payload, clob_result.payload)
        insert_fee_validation(conn, run_id, mode, sig, fee_policy)
        if fee_policy["fee_crosscheck_status"] != "official" and fee_policy["fee_crosscheck_status"] != "disabled":
            append_audit(conn, mode, run_id, "entry_fee_policy_rejected", {"signal_id": sig["signal_id"], "status": fee_policy["fee_crosscheck_status"], "details": fee_policy.get("fee_conflict_details")}, "warning", now)
            results.append({"signal_id": sig["signal_id"], "status": "fee_policy_rejected"})
            continue
        validation = validate_token_mapping(dict(sig), gamma_result.payload, clob_result.payload, book)
        insert_token_validation(conn, run_id, mode, sig, validation)
        if not validation["mapping_valid"]:
            append_audit(conn, mode, run_id, "token_mapping_rejected", {"signal_id": sig["signal_id"], "errors": validation["errors"]}, "warning", now)
            results.append({"signal_id": sig["signal_id"], "status": "mapping_rejected"})
            continue
        duplicate = conn.execute("SELECT 1 FROM entry_fills WHERE run_id=? AND signal_id=? AND snapshot_id=?", (run_id, sig["signal_id"], snapshot_id)).fetchone()
        if duplicate:
            continue
        requested_usd = dec(state["remaining_entry_usd"])
        buy = consume_buy_levels(ask_levels, requested_usd, dec(sig["max_entry_price"]), book["min_order_size"])
        if buy["filled_shares"] <= ZERO:
            conn.execute(
                "INSERT INTO entry_order_state(signal_id,token_id,updated_at_utc,intended_usd,filled_entry_usd,remaining_entry_usd,filled_entry_shares,entry_status,max_entry_price,entry_deadline_utc,last_entry_attempt_at,last_attempt_reason,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sig["signal_id"], sig["token_id"], now_iso(now), sig["intended_usd"], state["filled_entry_usd"], state["remaining_entry_usd"], state["filled_entry_shares"], state["entry_status"], sig["max_entry_price"], sig["entry_deadline_utc"], now_iso(now), buy["status"], mode),
            )
            results.append({"signal_id": sig["signal_id"], "status": buy["status"], "snapshot_id": snapshot_id})
            continue
        fee_calc = calculate_fee("buy", buy["filled_shares"], buy["vwap"], fee_policy)
        if fee_calc["net_cost_or_proceeds"] is None:
            continue
        fill_id = id_for("entry", {"run_id": run_id, "signal_id": sig["signal_id"], "snapshot_id": snapshot_id, "gross": buy["filled_usd"]})
        new_filled_usd = dec(state["filled_entry_usd"]) + buy["filled_usd"]
        new_filled_shares = dec(state["filled_entry_shares"]) + buy["filled_shares"]
        entry_status = "filled" if new_filled_usd >= dec(sig["intended_usd"]) - EPS else "partial"
        sm = snapshot_meta(conn, snapshot_id, mode)
        conn.execute(
            "INSERT INTO entry_fills(entry_fill_id,run_id,lock_id,signal_id,signal_registration_evidence_id,gamma_market_evidence_id,clob_market_evidence_id,orderbook_evidence_id,event_key,token_id,snapshot_id,filled_at_utc,gross_entry_cost,entry_fee,net_entry_cost,filled_shares,entry_vwap,fee_status,best_bid,best_ask,spread,complete_fill,unfilled_usd_after_fill,depth_levels_json,adapter_name,adapter_version,adapter_code_hash,data_source,run_environment,raw_response_hash,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (fill_id, run_id, lock_id, sig["signal_id"], sig["registration_evidence_id"], gamma_eid, clob_eid, ob_eid, sig["event_key"], sig["token_id"], snapshot_id, now_iso(now), dstr(fee_calc["gross_notional"]), dstr(fee_calc["official_fee"]), dstr(fee_calc["net_cost_or_proceeds"]), dstr(buy["filled_shares"]), dstr(buy["vwap"]), fee_calc["fee_status"], None if book["best_bid"] is None else dstr(book["best_bid"]), None if book["best_ask"] is None else dstr(book["best_ask"]), None if book["spread"] is None else dstr(book["spread"]), int(entry_status == "filled"), dstr(max(dec(sig["intended_usd"]) - new_filled_usd, ZERO)), stable_json(buy["levels"]), sm["adapter_name"], sm["adapter_version"], sm["adapter_code_hash"], sm["data_source"], sm["run_environment"], sm["raw_response_hash"], mode),
        )
        lot_ids: list[str] = []
        for strategy_id in STRATEGY_IDS:
            lot_id = id_for("lot", {"strategy_id": strategy_id, "entry_fill_id": fill_id})
            lot_ids.append(lot_id)
            conn.execute(
                "INSERT INTO strategy_lots(lot_id,run_id,strategy_id,signal_id,event_key,token_id,entry_fill_id,created_at_utc,entry_shares,gross_entry_cost,entry_fee,net_entry_cost,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (lot_id, run_id, strategy_id, sig["signal_id"], sig["event_key"], sig["token_id"], fill_id, now_iso(now), dstr(buy["filled_shares"]), dstr(fee_calc["gross_notional"]), dstr(fee_calc["official_fee"]), dstr(fee_calc["net_cost_or_proceeds"]), mode),
            )
        update_entry_fill_replay_fields(conn, fill_id, book, buy, fee_calc, fee_policy, requested_usd, entry_status, lot_ids)
        conn.execute(
            "INSERT INTO entry_order_state(signal_id,token_id,updated_at_utc,intended_usd,filled_entry_usd,remaining_entry_usd,filled_entry_shares,entry_status,max_entry_price,entry_deadline_utc,last_entry_attempt_at,last_attempt_reason,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sig["signal_id"], sig["token_id"], now_iso(now), sig["intended_usd"], dstr(new_filled_usd), dstr(max(dec(sig["intended_usd"]) - new_filled_usd, ZERO)), dstr(new_filled_shares), entry_status, sig["max_entry_price"], sig["entry_deadline_utc"], now_iso(now), entry_status, mode),
        )
        results.append({"signal_id": sig["signal_id"], "status": entry_status, "snapshot_id": snapshot_id, "filled_shares": dstr(buy["filled_shares"])})

    ordered_for_exit = sorted(signal_bundles, key=lambda item: (item[0]["created_at_utc"], item[0]["signal_id"]))
    for strategy_id in STRATEGY_IDS:
        strategy = STRATEGIES[strategy_id]
        if strategy["multiple"] is None:
            continue
        bid_levels = [dict(level) for level in book["bids"]]
        exit_items: list[tuple[sqlite3.Row, sqlite3.Row | None, dict[str, Decimal | None]]] = []
        for sig, _, _ in ordered_for_exit:
            if is_settled(conn, sig["signal_id"], strategy_id, mode):
                continue
            position = signal_position_conn(conn, sig["signal_id"], strategy_id, mode)
            if position["shares"] <= EPS or position["avg_cost"] is None:  # type: ignore[operator]
                continue
            trigger = latest_trigger(conn, sig["signal_id"], strategy_id, str(strategy["stage"]), mode)
            exit_items.append((sig, trigger, position))
        exit_items.sort(key=lambda item: (strategy_id, item[1]["trigger_created_at"] if item[1] else "", item[0]["created_at_utc"], item[0]["signal_id"], item[1]["trigger_id"] if item[1] else ""))
        for sig, trigger, position in exit_items:
            matched_bundle = next((item for item in signal_bundles if item[0]["signal_id"] == sig["signal_id"]), None)
            if matched_bundle:
                _, gamma_result, clob_result = matched_bundle
                gamma_eid = record_http_evidence(conn, mode, "gamma_market", run_id, sig["signal_id"], sig["token_id"], sig["condition_id"], sig["market_slug"], gamma_result)
                clob_eid = record_http_evidence(conn, mode, "clob_market", run_id, sig["signal_id"], sig["token_id"], sig["condition_id"], sig["market_slug"], clob_result)
                fee_policy = extract_fee_policy(gamma_result.payload, clob_result.payload)
            else:
                gamma_eid, clob_eid = first_gamma_eid, first_clob_eid
                fee_policy = extract_fee_policy(first_gamma.payload, first_clob.payload)
            if trigger and trigger["trigger_status"] == "completed":
                continue
            planned = dec(trigger["trigger_remaining_shares"]) if trigger else position["shares"] * strategy["fraction"]  # type: ignore[operator]
            threshold = dec(trigger["threshold_price"]) if trigger else position["avg_cost"] * strategy["multiple"]  # type: ignore[operator]
            probe = consume_sell_levels([dict(level) for level in bid_levels], planned, book["min_order_size"], mutate=False)
            if probe["filled_shares"] <= ZERO or probe["vwap"] is None or probe["vwap"] < threshold:
                results.append({"signal_id": sig["signal_id"], "strategy_id": strategy_id, "status": "not_triggered", "threshold": dstr(threshold), "vwap": None if probe["vwap"] is None else dstr(probe["vwap"])})
                continue
            trigger = trigger or create_trigger(conn, run_id, mode, sig, strategy_id, position, now)
            duplicate = conn.execute("SELECT 1 FROM exit_fills WHERE run_id=? AND trigger_id=? AND snapshot_id=?", (run_id, trigger["trigger_id"], snapshot_id)).fetchone()
            if duplicate:
                continue
            sell = consume_sell_levels(bid_levels, planned, book["min_order_size"], mutate=True)
            if sell["filled_shares"] <= ZERO:
                continue
            fee_calc = calculate_fee("sell", sell["filled_shares"], sell["vwap"], fee_policy)
            if fee_calc["net_cost_or_proceeds"] is None:
                continue
            fill_id = id_for("exit", {"run_id": run_id, "trigger_id": trigger["trigger_id"], "snapshot_id": snapshot_id, "shares": sell["filled_shares"]})
            sm = snapshot_meta(conn, snapshot_id, mode)
            conn.execute(
                "INSERT INTO exit_fills(exit_fill_id,run_id,lock_id,trigger_id,signal_id,strategy_id,signal_registration_evidence_id,gamma_market_evidence_id,clob_market_evidence_id,orderbook_evidence_id,trigger_stage_id,event_key,token_id,snapshot_id,filled_at_utc,planned_sell_shares,filled_shares,gross_exit_proceeds,exit_fee,net_exit_proceeds,exit_vwap,fee_status,best_bid,best_ask,spread,complete_fill,unfilled_trigger_shares_after_fill,depth_levels_json,adapter_name,adapter_version,adapter_code_hash,data_source,run_environment,raw_response_hash,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (fill_id, run_id, lock_id, trigger["trigger_id"], sig["signal_id"], strategy_id, sig["registration_evidence_id"], gamma_eid, clob_eid, ob_eid, trigger["trigger_stage_id"], sig["event_key"], sig["token_id"], snapshot_id, now_iso(now), dstr(planned), dstr(sell["filled_shares"]), dstr(fee_calc["gross_notional"]), dstr(fee_calc["official_fee"]), dstr(fee_calc["net_cost_or_proceeds"]), dstr(sell["vwap"]), fee_calc["fee_status"], None if book["best_bid"] is None else dstr(book["best_bid"]), None if book["best_ask"] is None else dstr(book["best_ask"]), None if book["spread"] is None else dstr(book["spread"]), int(sell["remaining_shares"] <= EPS), dstr(max(planned - sell["filled_shares"], ZERO)), stable_json(sell["levels"]), sm["adapter_name"], sm["adapter_version"], sm["adapter_code_hash"], sm["data_source"], sm["run_environment"], sm["raw_response_hash"], mode),
            )
            allocate_fifo(conn, run_id, mode, sig["signal_id"], strategy_id, sell["filled_shares"], fee_calc["gross_notional"], fee_calc["official_fee"], fee_calc["net_cost_or_proceeds"], fill_id, trigger["trigger_id"])
            update_trigger(conn, trigger, sell["filled_shares"], now)
            update_exit_fill_replay_fields(conn, fill_id, book, sell, fee_calc, fee_policy, planned, "filled" if sell["remaining_shares"] <= EPS else "partial")
            results.append({"signal_id": sig["signal_id"], "strategy_id": strategy_id, "status": "exit_filled", "filled_shares": dstr(sell["filled_shares"])})
    return results


def settle_signal_with_market(
    conn: sqlite3.Connection,
    run_id: str,
    mode: str,
    sig: sqlite3.Row,
    gamma_market: dict[str, Any],
    clob_info: dict[str, Any],
    source_endpoint: str,
    now: datetime | None = None,
    root: Path = PROJECT_ROOT,
    config_path: Path | None = None,
    clob_public_info: dict[str, Any] | None = None,
    lock_id: str = "",
    gamma_market_evidence_id: str = "",
    clob_market_evidence_id: str = "",
    clob_public_market_evidence_id: str = "",
) -> list[dict[str, Any]]:
    pairs = clob_token_pairs(clob_public_info or {}) or clob_token_pairs(clob_info) or gamma_token_pairs(gamma_market)
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
        meta = source_meta(root, config_path or (root / "config/forward_simulation_v5_1_8.yaml"), mode, source_endpoint, evidence["raw_response_hash"])
        raw_clob_payload = {"clob_markets": clob_info, "clob_public_market": clob_public_info or {}}
        settlement_evidence_id = gamma_market_evidence_id
        conn.execute(
            "INSERT OR IGNORE INTO settlements(settlement_id,run_id,lock_id,signal_id,strategy_id,event_key,condition_id,token_id,source_endpoint,source_reference,gamma_market_evidence_id,clob_market_evidence_id,clob_public_market_evidence_id,settlement_evidence_id,raw_http_sha256,canonical_json_sha256,observed_at_utc,recorded_at_utc,raw_response,raw_response_hash,market_status,resolution_status,winning_asset_id,winning_outcome,token_settlement_values,evidence_valid,finality_status,evidence_tier,conflict_details,uma_status,raw_clob_response,raw_clob_response_hash,adapter_name,adapter_version,adapter_code_hash,data_source,run_environment,settlement_value,remaining_shares_settled,gross_settlement_proceeds,settlement_fee,net_settlement_proceeds,fee_status,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (settlement_id, run_id, lock_id, sig["signal_id"], strategy_id, sig["event_key"], sig["condition_id"], sig["token_id"], source_endpoint, sig["market_slug"], gamma_market_evidence_id, clob_market_evidence_id, clob_public_market_evidence_id, settlement_evidence_id, evidence["raw_response_hash"], content_hash(gamma_market), now_iso(now), now_iso(now), stable_json(gamma_market), evidence["raw_response_hash"], evidence["market_status"], str(evidence.get("resolution_status") or ""), evidence["winning_asset_id"], evidence["winning_outcome"], stable_json(evidence["token_settlement_values"]), int(True), evidence.get("finality_status", ""), evidence.get("evidence_tier", ""), evidence.get("conflict_details", ""), evidence.get("uma_status", ""), stable_json(raw_clob_payload), content_hash(raw_clob_payload), meta["adapter_name"], meta["adapter_version"], meta["adapter_code_hash"], meta["data_source"], meta["run_environment"], dstr(value), dstr(remaining), dstr(gross), dstr(fee_calc["official_fee"]), dstr(fee_calc["net_cost_or_proceeds"]), fee_calc["fee_status"], mode),
        )
        for lot in lots:
            ratio = lot["open_shares"] / remaining if remaining > ZERO else ZERO
            conn.execute(
                "INSERT OR IGNORE INTO settlement_allocations(settlement_allocation_id,run_id,settlement_id,strategy_id,signal_id,event_key,token_id,lot_id,settled_shares,gross_settlement_proceeds,settlement_fee,net_settlement_proceeds,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (id_for("set_alloc", {"settlement_id": settlement_id, "lot_id": lot["lot_id"]}), run_id, settlement_id, strategy_id, sig["signal_id"], sig["event_key"], sig["token_id"], lot["lot_id"], dstr(lot["open_shares"]), dstr(gross * ratio), "0", dstr(gross * ratio), mode),
            )
        results.append({"signal_id": sig["signal_id"], "strategy_id": strategy_id, "status": "settled", "value": dstr(value)})
    return results


def write_resolved_raw_evidence(root: Path, config: dict[str, Any], mode: str, run_id: str, sig: sqlite3.Row, gamma_result: Any, clob_result: Any, now: datetime | None = None, clob_public_result: Any | None = None) -> dict[str, Any]:
    pairs = clob_token_pairs(clob_public_result.payload if clob_public_result else {}) or clob_token_pairs(clob_result.payload) or gamma_token_pairs(gamma_result.payload)
    evidence = parse_settlement_evidence(gamma_result.payload, pairs)
    payload = {
        "version": VERSION,
        "mode": mode,
        "run_id": run_id,
        "signal_id": sig["signal_id"],
        "market_slug": sig["market_slug"],
        "condition_id": sig["condition_id"],
        "token_id": sig["token_id"],
        "observed_at_utc": now_iso(now),
        "gamma": {
            "endpoint": gamma_result.url,
            "status_code": gamma_result.status_code,
            "received_at_utc": gamma_result.received_at_utc,
            "raw_text_sha256": sha256_text(gamma_result.raw_text),
            "payload_sha256": content_hash(gamma_result.payload),
            "raw_text": gamma_result.raw_text,
            "payload": gamma_result.payload,
        },
        "clob": {
            "endpoint": clob_result.url,
            "status_code": clob_result.status_code,
            "received_at_utc": clob_result.received_at_utc,
            "raw_text_sha256": sha256_text(clob_result.raw_text),
            "payload_sha256": content_hash(clob_result.payload),
            "raw_text": clob_result.raw_text,
            "payload": clob_result.payload,
        },
        "clob_public_market": None
        if clob_public_result is None
        else {
            "endpoint": clob_public_result.url,
            "status_code": clob_public_result.status_code,
            "received_at_utc": clob_public_result.received_at_utc,
            "raw_text_sha256": sha256_text(clob_public_result.raw_text),
            "payload_sha256": content_hash(clob_public_result.payload),
            "raw_text": clob_public_result.raw_text,
            "payload": clob_public_result.payload,
        },
        "settlement_evidence": evidence,
        "token_settlement_values": evidence.get("token_settlement_values", {}),
    }
    name = f"{run_id}_{sig['signal_id']}_{content_hash(payload)[:12]}.json".replace("/", "_")
    targets = [data_dir(root, mode, config) / "resolved_market_raw" / name]
    if mode == LIVE:
        targets.append(rc7_dir(root) / "resolved_market_raw" / name)
    for target in targets:
        write_json(target, payload)
    return payload


def read_monitor_flags(root: Path, mode: str, config_path: Path) -> dict[str, str]:
    config = load_config(config_path)
    db = init_ledger(root, mode, config_path)
    conn = connect(db)
    try:
        return {"paused": get_state(conn, "paused", "false"), "stopped": get_state(conn, "stopped", "false")}
    finally:
        conn.close()


ORDERBOOK_REPLAY_CODES = [
    "ORDERBOOK_RAW_HASH_MISMATCH",
    "ORDERBOOK_NORMALIZED_HASH_MISMATCH",
    "ORDERBOOK_BIDS_MISMATCH",
    "ORDERBOOK_ASKS_MISMATCH",
    "ORDERBOOK_BEST_BID_MISMATCH",
    "ORDERBOOK_BEST_ASK_MISMATCH",
    "ORDERBOOK_SPREAD_MISMATCH",
    "ORDERBOOK_DEPTH_TOTAL_MISMATCH",
    "ORDERBOOK_ASSET_ID_MISMATCH",
    "ORDERBOOK_CONDITION_ID_MISMATCH",
    "ORDERBOOK_TICK_VIOLATION",
    "ORDERBOOK_NEGATIVE_SIZE",
    "ORDERBOOK_CROSSED",
    "ORDERBOOK_RAW_EVIDENCE_MISSING",
]

FILL_REPLAY_CODES = [
    "FILL_TRACE_MISMATCH",
    "FILL_SHARES_MISMATCH",
    "FILL_NOTIONAL_MISMATCH",
    "FILL_VWAP_MISMATCH",
    "FILL_LEVEL_COUNT_MISMATCH",
    "FILL_UNFILLED_AMOUNT_MISMATCH",
    "FILL_FULLY_FILLED_FLAG_MISMATCH",
    "FILL_WRONG_BOOK_SIDE",
    "FILL_VISIBLE_DEPTH_EXCEEDED",
    "FILL_SHARED_DEPTH_EXCEEDED",
    "FILL_ORDERING_MISMATCH",
    "FILL_TRACE_MISSING",
    "FILL_SNAPSHOT_NOT_FOUND",
    "FILL_FEE_MISMATCH",
    "FILL_NET_AMOUNT_MISMATCH",
    "FILL_GROSS_AMOUNT_MISMATCH",
    "FILL_FEE_SOURCE_MISMATCH",
    "FILL_FEE_RATE_MISMATCH",
    "FILL_FEE_EXPONENT_MISMATCH",
    "FILL_FEE_STATUS_MISMATCH",
    "FILL_UNSUPPORTED_FEE_EXPONENT",
    "FILL_UNKNOWN_FEE_USED",
    "FILL_CONFLICT_FEE_USED",
    "POSITION_SHARE_CONSERVATION_FAILED",
    "POSITION_COST_BASIS_MISMATCH",
    "POSITION_PNL_MISMATCH",
    "TRIGGER_TARGET_EXCEEDED",
    "TRIGGER_STATUS_MISMATCH",
    "SIGNAL_TOKEN_PNL_MISMATCH",
    "EVENT_PNL_MISMATCH",
    "TOTAL_LEDGER_PNL_MISMATCH",
]

END_TO_END_REPLAY_CODES = [
    "MARKET_EVIDENCE_MISSING",
    "MARKET_RAW_HTTP_HASH_MISMATCH",
    "MARKET_CANONICAL_HASH_MISMATCH",
    "MARKET_RESPONSE_PARSE_FAILED",
    "ORDERBOOK_RAW_HTTP_HASH_MISMATCH",
    "ORDERBOOK_CANONICAL_HASH_MISMATCH",
    "SETTLEMENT_RAW_HTTP_HASH_MISMATCH",
    "SIGNAL_RAW_HASH_MISMATCH",
    "SIGNAL_CANONICAL_HASH_MISMATCH",
    "SIGNAL_FIELD_MISMATCH",
    "SIGNAL_INTENDED_USD_MISMATCH",
    "SIGNAL_MAX_ENTRY_PRICE_MISMATCH",
    "SIGNAL_ENTRY_DEADLINE_MISMATCH",
    "SIGNAL_EVENT_KEY_MISMATCH",
    "SIGNAL_BUCKET_MISMATCH",
    "SIGNAL_REGISTRATION_EVIDENCE_MISSING",
    *ENTRY_STATE_CODES,
    "LOT_ENTRY_SHARES_MISMATCH",
    "LOT_ENTRY_COST_MISMATCH",
    "LOT_ENTRY_FEE_MISMATCH",
    "LOT_SOLD_SHARES_MISMATCH",
    "LOT_REMAINING_SHARES_MISMATCH",
    "LOT_EXIT_PROCEEDS_MISMATCH",
    "LOT_SETTLEMENT_PROCEEDS_MISMATCH",
    "LOT_PNL_MISMATCH",
    "EXIT_ALLOCATION_SHARES_MISMATCH",
    "EXIT_ALLOCATION_GROSS_PROCEEDS_MISMATCH",
    "EXIT_ALLOCATION_FEE_MISMATCH",
    "EXIT_ALLOCATION_NET_PROCEEDS_MISMATCH",
    "EXIT_ALLOCATION_ORDER_MISMATCH",
    "SETTLEMENT_ALLOCATION_SHARES_MISMATCH",
    "SETTLEMENT_ALLOCATION_VALUE_MISMATCH",
    "SETTLEMENT_ALLOCATION_NET_PROCEEDS_MISMATCH",
    "STRATEGY_PNL_MISMATCH",
    "MARKET_CONSTRAINT_SOURCE_MISMATCH",
    "MARKET_TICK_SIZE_MISMATCH",
    "MARKET_MIN_ORDER_SIZE_MISMATCH",
    "MARKET_CONSTRAINT_HASH_MISMATCH",
    "FILL_TICK_SIZE_MISMATCH",
    "FILL_MIN_ORDER_SIZE_MISMATCH",
    "MARKET_CONSTRAINT_CONFLICT_IGNORED",
    "INCOMPLETE_TAKE_PROFIT_MISMATCH",
]


def inc(checks: dict[str, Any], code: str, amount: int = 1) -> None:
    checks[code] = int(checks.get(code, 0) or 0) + amount


def same_decimal(a: Any, b: Any) -> bool:
    try:
        return dec(a) == dec(b)
    except Exception:
        return False


def same_json(a: str, b: Any) -> bool:
    try:
        return stable_json(json.loads(a or "")) == stable_json(b)
    except Exception:
        return False


def fee_policy_from_fill_row(row: sqlite3.Row) -> dict[str, Any]:
    status = str(row["fee_status"])
    if status == "official":
        return {"fee_status": "official", "fee_rate": dec(row["fee_rate"]), "clob_fee_exponent": dec(row["fee_exponent"] or "1")}
    if status == "disabled":
        return {"fee_status": "disabled", "fee_rate": ZERO, "clob_fee_exponent": dec(row["fee_exponent"] or "1")}
    return {"fee_status": status, "fee_rate": None, "clob_fee_exponent": None}


def fee_policy_from_fill_evidence(conn: sqlite3.Connection, row: sqlite3.Row, checks: dict[str, Any]) -> dict[str, Any] | None:
    gamma_id = str(row["gamma_market_evidence_id"] or "")
    clob_id = str(row["clob_market_evidence_id"] or "")
    gamma_payload = evidence_payload(conn, gamma_id, checks, "MARKET") if gamma_id else None
    clob_payload = evidence_payload(conn, clob_id, checks, "MARKET") if clob_id else None
    if gamma_payload is None or clob_payload is None:
        inc(checks, "FILL_FEE_SOURCE_MISMATCH")
        return None
    policy = extract_fee_policy(gamma_payload, clob_payload)
    expected_status = str(policy.get("fee_crosscheck_status") or policy.get("fee_status") or "")
    if expected_status == "official":
        expected_rate = policy.get("fee_rate")
        expected_exponent = policy.get("clob_fee_exponent")
    elif expected_status == "disabled":
        expected_rate = ZERO
        expected_exponent = policy.get("clob_fee_exponent") or dec("1")
    else:
        expected_rate = None
        expected_exponent = policy.get("clob_fee_exponent")
    if str(row["fee_status"]) != expected_status:
        inc(checks, "FILL_FEE_STATUS_MISMATCH")
        inc(checks, "FILL_FEE_SOURCE_MISMATCH")
    if expected_rate is None:
        if expected_status == "unknown":
            inc(checks, "FILL_UNKNOWN_FEE_USED")
        if expected_status == "conflict":
            inc(checks, "FILL_CONFLICT_FEE_USED")
    elif row["fee_rate"] not in ("", None) and not same_decimal(row["fee_rate"], expected_rate):
        inc(checks, "FILL_FEE_RATE_MISMATCH")
        inc(checks, "FILL_FEE_SOURCE_MISMATCH")
    if expected_exponent is not None and row["fee_exponent"] not in ("", None) and not same_decimal(row["fee_exponent"], expected_exponent):
        inc(checks, "FILL_FEE_EXPONENT_MISMATCH")
        inc(checks, "FILL_FEE_SOURCE_MISMATCH")
    return {
        "fee_status": expected_status,
        "fee_rate": expected_rate,
        "clob_fee_exponent": expected_exponent,
        "fee_crosscheck_status": expected_status,
    }


def compare_fill_amounts(checks: dict[str, Any], row: sqlite3.Row, result: dict[str, Any], action: str, fee_calc: dict[str, Any]) -> None:
    share_col = "filled_shares"
    vwap_col = "entry_vwap" if action == "buy" else "exit_vwap"
    gross_col = "gross_entry_cost" if action == "buy" else "gross_exit_proceeds"
    fee_col = "entry_fee" if action == "buy" else "exit_fee"
    net_col = "net_entry_cost" if action == "buy" else "net_exit_proceeds"
    unfilled_key = "remaining_usd" if action == "buy" else "remaining_shares"
    complete_col = "complete_fill"
    if not same_decimal(row[share_col], result["filled_shares"]):
        inc(checks, "FILL_SHARES_MISMATCH")
    if not same_decimal(row[gross_col], result["filled_usd"]) or (row["filled_notional"] and not same_decimal(row["filled_notional"], result["filled_usd"])):
        inc(checks, "FILL_NOTIONAL_MISMATCH")
        inc(checks, "FILL_GROSS_AMOUNT_MISMATCH")
    if result["vwap"] is None or not same_decimal(row[vwap_col], result["vwap"]):
        inc(checks, "FILL_VWAP_MISMATCH")
    if row["levels_consumed_count"] != len(result.get("levels") or []):
        inc(checks, "FILL_LEVEL_COUNT_MISMATCH")
    if not same_decimal(row["unfilled_amount"], result[unfilled_key]):
        inc(checks, "FILL_UNFILLED_AMOUNT_MISMATCH")
    expected_full = int(dec(result[unfilled_key]) <= EPS)
    if int(row[complete_col]) != expected_full or int(row["fully_filled"]) != expected_full:
        inc(checks, "FILL_FULLY_FILLED_FLAG_MISMATCH")
    if not str(row["consumed_levels_json"] or "").strip():
        inc(checks, "FILL_TRACE_MISSING")
    elif not same_json(row["consumed_levels_json"], result.get("levels") or []):
        inc(checks, "FILL_TRACE_MISMATCH")
    if not same_decimal(row[gross_col], fee_calc["gross_notional"]) or (row["gross_notional"] and not same_decimal(row["gross_notional"], fee_calc["gross_notional"])):
        inc(checks, "FILL_GROSS_AMOUNT_MISMATCH")
    if not same_decimal(row[fee_col], fee_calc["official_fee"]) or (row["official_fee"] and not same_decimal(row["official_fee"], fee_calc["official_fee"])):
        inc(checks, "FILL_FEE_MISMATCH")
    if not same_decimal(row[net_col], fee_calc["net_cost_or_proceeds"]) or (row["net_cost_or_proceeds"] and not same_decimal(row["net_cost_or_proceeds"], fee_calc["net_cost_or_proceeds"])):
        inc(checks, "FILL_NET_AMOUNT_MISMATCH")


def audit_http_evidence_replay(conn: sqlite3.Connection, mode: str, checks: dict[str, Any]) -> None:
    for code in END_TO_END_REPLAY_CODES:
        checks.setdefault(code, 0)
    for ev in conn.execute("SELECT * FROM http_evidence WHERE mode=?", (mode,)):
        etype = str(ev["evidence_type"])
        prefix = "ORDERBOOK" if etype == "orderbook" else "SETTLEMENT" if "settlement" in etype else "MARKET"
        evidence_payload(conn, ev["evidence_id"], checks, prefix)


def audit_signal_registration_replay(conn: sqlite3.Connection, mode: str, checks: dict[str, Any]) -> None:
    for code in END_TO_END_REPLAY_CODES:
        checks.setdefault(code, 0)
    for sig in conn.execute("SELECT * FROM signals WHERE mode=?", (mode,)):
        ev_id = str(sig["registration_evidence_id"] or "")
        ev = conn.execute("SELECT * FROM signal_registration_evidence WHERE evidence_id=? AND mode=?", (ev_id, mode)).fetchone() if ev_id else None
        if ev is None:
            inc(checks, "SIGNAL_REGISTRATION_EVIDENCE_MISSING")
            continue
        raw_bytes = bytes(ev["original_signal_payload_bytes"])
        raw_sha = hashlib.sha256(raw_bytes).hexdigest()
        if raw_sha != ev["original_signal_payload_sha256"] or (sig["original_signal_payload_sha256"] and sig["original_signal_payload_sha256"] != raw_sha):
            inc(checks, "SIGNAL_RAW_HASH_MISMATCH")
        try:
            raw_payload = json.loads(raw_bytes.decode("utf-8"))
        except Exception:
            inc(checks, "SIGNAL_RAW_HASH_MISMATCH")
            raw_payload = {}
        try:
            normalized = json.loads(ev["normalized_signal_fields_json"])
        except Exception:
            normalized = {}
            inc(checks, "SIGNAL_CANONICAL_HASH_MISMATCH")
        canonical = canonical_signal_from_payload(normalized or raw_payload)
        canonical_json = stable_json(canonical)
        canonical_sha = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
        if canonical_sha != ev["canonical_signal_sha256"] or (sig["canonical_signal_sha256"] and sig["canonical_signal_sha256"] != canonical_sha) or sig["signal_hash"] != canonical_sha:
            inc(checks, "SIGNAL_CANONICAL_HASH_MISMATCH")
        expected_event = make_event_key(canonical["city"], canonical["weather_date_local"], canonical["weather_metric"])
        expected_bucket = f"{canonical['bucket_type']}:{canonical['temperature_threshold']}{canonical['temperature_unit']}"
        field_pairs = {
            "created_at_utc": canonical["created_at_utc"],
            "city": canonical["city"],
            "weather_date_local": canonical["weather_date_local"],
            "weather_metric": canonical["weather_metric"],
            "market_slug": canonical["market_slug"],
            "condition_id": canonical["condition_id"],
            "token_id": canonical["token_id"],
            "outcome": canonical["outcome"],
            "side": canonical["side"],
            "source": canonical["source"],
            "notes": canonical["notes"],
        }
        for field, expected in field_pairs.items():
            actual = str(sig[field] or "")
            if actual != expected and not (field == "side" and actual.upper() == expected):
                inc(checks, "SIGNAL_FIELD_MISMATCH")
        if not same_decimal(sig["intended_usd"], canonical["intended_usd"]):
            inc(checks, "SIGNAL_INTENDED_USD_MISMATCH")
        if not same_decimal(sig["max_entry_price"], canonical["max_entry_price"]):
            inc(checks, "SIGNAL_MAX_ENTRY_PRICE_MISMATCH")
        if str(sig["entry_deadline_utc"]) != canonical["entry_deadline_utc"]:
            inc(checks, "SIGNAL_ENTRY_DEADLINE_MISMATCH")
        if sig["event_key"] != expected_event:
            inc(checks, "SIGNAL_EVENT_KEY_MISMATCH")
        if sig["temperature_bucket"] != expected_bucket:
            inc(checks, "SIGNAL_BUCKET_MISMATCH")


def audit_entry_state_replay(conn: sqlite3.Connection, mode: str, checks: dict[str, Any]) -> None:
    for code in ENTRY_STATE_CODES:
        checks.setdefault(code, 0)
    for sig in conn.execute("SELECT * FROM signals WHERE mode=?", (mode,)):
        for code in entry_state_cache_errors(conn, sig, mode, None):
            inc(checks, code)
            inc(checks, "ENTRY_STATE_CACHE_CORRUPTED")


def audit_orderbook_snapshot_replay(conn: sqlite3.Connection, mode: str, checks: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    books: dict[tuple[str, str], dict[str, Any]] = {}
    for code in ORDERBOOK_REPLAY_CODES:
        checks.setdefault(code, 0)
    for snap in conn.execute("SELECT * FROM orderbook_snapshots WHERE mode=?", (mode,)):
        raw_book = None
        gamma_payload = None
        if snap["gamma_market_evidence_id"]:
            gamma_payload = evidence_payload(conn, snap["gamma_market_evidence_id"], checks, "MARKET")
        if snap["orderbook_evidence_id"]:
            raw_book = evidence_payload(conn, snap["orderbook_evidence_id"], checks, "ORDERBOOK")
        raw_text = str(snap["raw_response"] or snap["raw_orderbook_json"] or "")
        if raw_book is None and not raw_text:
            inc(checks, "ORDERBOOK_RAW_EVIDENCE_MISSING")
            continue
        if raw_book is None:
            raw_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
            if snap["raw_response_sha256"] and raw_hash != snap["raw_response_sha256"]:
                inc(checks, "ORDERBOOK_RAW_HASH_MISMATCH")
            if snap["raw_response_hash"] and raw_hash != snap["raw_response_hash"]:
                inc(checks, "ORDERBOOK_RAW_HASH_MISMATCH")
            try:
                raw_book = json.loads(raw_text)
            except Exception:
                inc(checks, "ORDERBOOK_RAW_EVIDENCE_MISSING")
                continue
        try:
            book = normalize_orderbook(raw_book, snap["token_id"], snap["condition_id"], gamma_payload)
        except AdapterError as exc:
            code = getattr(exc, "category", str(exc))
            if code == "invalid_tick":
                inc(checks, "ORDERBOOK_TICK_VIOLATION")
            elif code == "invalid_size":
                inc(checks, "ORDERBOOK_NEGATIVE_SIZE")
            elif code == "crossed_book":
                inc(checks, "ORDERBOOK_CROSSED")
            elif code == "asset_mismatch":
                inc(checks, "ORDERBOOK_ASSET_ID_MISMATCH")
            elif code == "condition_mismatch":
                inc(checks, "ORDERBOOK_CONDITION_ID_MISMATCH")
            else:
                inc(checks, "ORDERBOOK_RAW_EVIDENCE_MISSING")
            continue
        books[(snap["run_id"], snap["snapshot_id"])] = book
        if snap["normalized_book_sha256"] and snap["normalized_book_sha256"] != book["normalized_book_sha256"]:
            inc(checks, "ORDERBOOK_NORMALIZED_HASH_MISMATCH")
        if snap["content_hash"] and snap["content_hash"] != book["normalized_book_sha256"]:
            inc(checks, "ORDERBOOK_NORMALIZED_HASH_MISMATCH")
        if snap["normalized_bids_json"] and not same_json(snap["normalized_bids_json"], book["bids"]):
            inc(checks, "ORDERBOOK_BIDS_MISMATCH")
        if snap["normalized_asks_json"] and not same_json(snap["normalized_asks_json"], book["asks"]):
            inc(checks, "ORDERBOOK_ASKS_MISMATCH")
        if (snap["best_bid"] or book["best_bid"] is not None) and not same_decimal(snap["best_bid"], book["best_bid"]):
            inc(checks, "ORDERBOOK_BEST_BID_MISMATCH")
        if (snap["best_ask"] or book["best_ask"] is not None) and not same_decimal(snap["best_ask"], book["best_ask"]):
            inc(checks, "ORDERBOOK_BEST_ASK_MISMATCH")
        if (snap["spread"] or book["spread"] is not None) and not same_decimal(snap["spread"], book["spread"]):
            inc(checks, "ORDERBOOK_SPREAD_MISMATCH")
        if int(snap["bid_levels_count"] or 0) != int(book["bid_depth_levels"]) or int(snap["ask_levels_count"] or 0) != int(book["ask_depth_levels"]):
            inc(checks, "ORDERBOOK_DEPTH_TOTAL_MISMATCH")
        if not same_decimal(snap["total_bid_shares"], book["total_bid_shares"]) or not same_decimal(snap["total_ask_shares"], book["total_ask_shares"]):
            inc(checks, "ORDERBOOK_DEPTH_TOTAL_MISMATCH")
        if str(raw_book.get("asset_id") or raw_book.get("token_id") or "") != str(snap["token_id"]):
            inc(checks, "ORDERBOOK_ASSET_ID_MISMATCH")
        if str(raw_book.get("market") or raw_book.get("condition_id") or raw_book.get("conditionId") or "").lower() != str(snap["condition_id"]).lower():
            inc(checks, "ORDERBOOK_CONDITION_ID_MISMATCH")
        market_constraints = {
            "condition_id": snap["condition_id"],
            "token_id": snap["token_id"],
            "market_slug": snap["market_slug"],
            "tick_size": book["tick_size"],
            "min_order_size": book["min_order_size"],
            "gamma_tick_size": book.get("gamma_tick_size"),
            "gamma_min_order_size": book.get("gamma_min_order_size"),
            "clob_tick_size": book.get("clob_tick_size"),
            "clob_min_order_size": book.get("clob_min_order_size"),
            "constraint_crosscheck_status": book.get("constraint_crosscheck_status"),
        }
        expected_constraints_hash = content_hash(market_constraints)
        if snap["market_constraints_hash"] != expected_constraints_hash:
            inc(checks, "MARKET_CONSTRAINT_HASH_MISMATCH")
        if snap["selected_tick_size"] and not same_decimal(snap["selected_tick_size"], book["tick_size"]):
            inc(checks, "MARKET_TICK_SIZE_MISMATCH")
        if snap["selected_min_order_size"] and not same_decimal(snap["selected_min_order_size"], book["min_order_size"]):
            inc(checks, "MARKET_MIN_ORDER_SIZE_MISMATCH")
        if snap["tick_size"] and not same_decimal(snap["tick_size"], book["tick_size"]):
            inc(checks, "MARKET_TICK_SIZE_MISMATCH")
        if snap["min_order_size"] and not same_decimal(snap["min_order_size"], book["min_order_size"]):
            inc(checks, "MARKET_MIN_ORDER_SIZE_MISMATCH")
        if str(book.get("constraint_crosscheck_status") or "") in {"conflict", "unknown", ""}:
            inc(checks, "MARKET_CONSTRAINT_CONFLICT_IGNORED")
    return books


def audit_fill_replay(conn: sqlite3.Connection, mode: str, checks: dict[str, Any], books: dict[tuple[str, str], dict[str, Any]]) -> None:
    for code in FILL_REPLAY_CODES:
        checks.setdefault(code, 0)
    for snap_key, book in books.items():
        run_id, snapshot_id = snap_key
        stable_entries = conn.execute(
            """
            SELECT ef.*, s.created_at_utc signal_created_at, s.max_entry_price
            FROM entry_fills ef
            JOIN signals s ON ef.signal_id=s.signal_id AND ef.mode=s.mode
            WHERE ef.mode=? AND ef.run_id=? AND ef.snapshot_id=?
            ORDER BY s.created_at_utc, s.signal_id
            """,
            (mode, run_id, snapshot_id),
        ).fetchall()
        row_order_entries = conn.execute("SELECT entry_fill_id FROM entry_fills WHERE mode=? AND run_id=? AND snapshot_id=? ORDER BY row_id", (mode, run_id, snapshot_id)).fetchall()
        if [r["entry_fill_id"] for r in row_order_entries] != [r["entry_fill_id"] for r in stable_entries]:
            inc(checks, "FILL_ORDERING_MISMATCH")
        ask_levels = [dict(level) for level in book["asks"]]
        for row in stable_entries:
            if row["action"] not in ("", "buy") or row["side"] not in ("", "ask"):
                inc(checks, "FILL_WRONG_BOOK_SIDE")
            if row["tick_size"] and not same_decimal(row["tick_size"], book["tick_size"]):
                inc(checks, "FILL_TICK_SIZE_MISMATCH")
            if row["min_order_size"] and not same_decimal(row["min_order_size"], book["min_order_size"]):
                inc(checks, "FILL_MIN_ORDER_SIZE_MISMATCH")
            requested = dec(row["requested_amount"] or row["requested_usd"] or row["gross_entry_cost"])
            before_shares = sum((x["size"] for x in ask_levels), ZERO)
            result = consume_buy_levels(ask_levels, requested, dec(row["max_entry_price"]), book["min_order_size"])
            if dec(row["filled_shares"]) - result["filled_shares"] > EPS or dec(row["filled_shares"]) > before_shares + EPS:
                inc(checks, "FILL_VISIBLE_DEPTH_EXCEEDED")
                inc(checks, "FILL_SHARED_DEPTH_EXCEEDED")
            policy = fee_policy_from_fill_evidence(conn, row, checks) or fee_policy_from_fill_row(row)
            if policy["fee_status"] == "unsupported_fee_exponent":
                inc(checks, "FILL_UNSUPPORTED_FEE_EXPONENT")
            if policy["fee_status"] == "unknown":
                inc(checks, "FILL_UNKNOWN_FEE_USED")
            if policy["fee_status"] == "conflict":
                inc(checks, "FILL_CONFLICT_FEE_USED")
            fee_calc = calculate_fee("buy", result["filled_shares"], result["vwap"] if result["vwap"] is not None else ZERO, policy)
            if fee_calc["official_fee"] is not None:
                compare_fill_amounts(checks, row, result, "buy", fee_calc)
        for strategy_id in STRATEGY_IDS:
            bid_levels = [dict(level) for level in book["bids"]]
            stable_exits = conn.execute(
                """
                SELECT xf.*, s.created_at_utc signal_created_at, lt.trigger_created_at
                FROM exit_fills xf
                JOIN signals s ON xf.signal_id=s.signal_id AND xf.mode=s.mode
                LEFT JOIN (
                  SELECT st.* FROM strategy_triggers st
                  JOIN (SELECT trigger_id,mode,MAX(row_id) row_id FROM strategy_triggers GROUP BY trigger_id,mode) latest
                    ON latest.row_id=st.row_id
                ) lt ON xf.trigger_id=lt.trigger_id AND xf.mode=lt.mode
                WHERE xf.mode=? AND xf.run_id=? AND xf.snapshot_id=? AND xf.strategy_id=?
                ORDER BY xf.strategy_id, lt.trigger_created_at, s.created_at_utc, s.signal_id, xf.trigger_id
                """,
                (mode, run_id, snapshot_id, strategy_id),
            ).fetchall()
            row_order_exits = conn.execute("SELECT exit_fill_id FROM exit_fills WHERE mode=? AND run_id=? AND snapshot_id=? AND strategy_id=? ORDER BY row_id", (mode, run_id, snapshot_id, strategy_id)).fetchall()
            if [r["exit_fill_id"] for r in row_order_exits] != [r["exit_fill_id"] for r in stable_exits]:
                inc(checks, "FILL_ORDERING_MISMATCH")
            for row in stable_exits:
                if row["action"] not in ("", "sell") or row["side"] not in ("", "bid"):
                    inc(checks, "FILL_WRONG_BOOK_SIDE")
                if row["tick_size"] and not same_decimal(row["tick_size"], book["tick_size"]):
                    inc(checks, "FILL_TICK_SIZE_MISMATCH")
                if row["min_order_size"] and not same_decimal(row["min_order_size"], book["min_order_size"]):
                    inc(checks, "FILL_MIN_ORDER_SIZE_MISMATCH")
                requested = dec(row["requested_amount"] or row["requested_shares"] or row["planned_sell_shares"])
                before_shares = sum((x["size"] for x in bid_levels), ZERO)
                result = consume_sell_levels(bid_levels, requested, book["min_order_size"], mutate=True)
                if dec(row["filled_shares"]) - result["filled_shares"] > EPS or dec(row["filled_shares"]) > before_shares + EPS:
                    inc(checks, "FILL_VISIBLE_DEPTH_EXCEEDED")
                    inc(checks, "FILL_SHARED_DEPTH_EXCEEDED")
                policy = fee_policy_from_fill_evidence(conn, row, checks) or fee_policy_from_fill_row(row)
                if policy["fee_status"] == "unsupported_fee_exponent":
                    inc(checks, "FILL_UNSUPPORTED_FEE_EXPONENT")
                if policy["fee_status"] == "unknown":
                    inc(checks, "FILL_UNKNOWN_FEE_USED")
                if policy["fee_status"] == "conflict":
                    inc(checks, "FILL_CONFLICT_FEE_USED")
                fee_calc = calculate_fee("sell", result["filled_shares"], result["vwap"] if result["vwap"] is not None else ZERO, policy)
                if fee_calc["official_fee"] is not None:
                    compare_fill_amounts(checks, row, result, "sell", fee_calc)
    for row in conn.execute("SELECT COUNT(*) c FROM entry_fills ef LEFT JOIN orderbook_snapshots ob ON ef.run_id=ob.run_id AND ef.snapshot_id=ob.snapshot_id AND ef.mode=ob.mode WHERE ef.mode=? AND ob.row_id IS NULL", (mode,)):
        if row["c"]:
            inc(checks, "FILL_SNAPSHOT_NOT_FOUND", row["c"])
    for row in conn.execute("SELECT COUNT(*) c FROM exit_fills xf LEFT JOIN orderbook_snapshots ob ON xf.run_id=ob.run_id AND xf.snapshot_id=ob.snapshot_id AND xf.mode=ob.mode WHERE xf.mode=? AND ob.row_id IS NULL", (mode,)):
        if row["c"]:
            inc(checks, "FILL_SNAPSHOT_NOT_FOUND", row["c"])


def audit_lots_allocations_and_ledger(conn: sqlite3.Connection, mode: str, checks: dict[str, Any]) -> None:
    for code in END_TO_END_REPLAY_CODES:
        checks.setdefault(code, 0)
    expected_lots: dict[str, dict[str, Decimal | str]] = {}
    for ef in conn.execute("SELECT * FROM entry_fills WHERE mode=? ORDER BY filled_at_utc,row_id", (mode,)):
        for strategy_id in STRATEGY_IDS:
            lot_id = id_for("lot", {"strategy_id": strategy_id, "entry_fill_id": ef["entry_fill_id"]})
            lot = conn.execute("SELECT * FROM strategy_lots WHERE lot_id=? AND mode=?", (lot_id, mode)).fetchone()
            if lot is None:
                inc(checks, "LOT_ENTRY_SHARES_MISMATCH")
                continue
            if not same_decimal(lot["entry_shares"], ef["filled_shares"]):
                inc(checks, "LOT_ENTRY_SHARES_MISMATCH")
            if not same_decimal(lot["gross_entry_cost"], ef["gross_entry_cost"]):
                inc(checks, "LOT_ENTRY_COST_MISMATCH")
            if not same_decimal(lot["entry_fee"], ef["entry_fee"]):
                inc(checks, "LOT_ENTRY_FEE_MISMATCH")
            expected_lots[lot_id] = {"entry_shares": dec(ef["filled_shares"]), "gross_entry_cost": dec(ef["gross_entry_cost"]), "entry_fee": dec(ef["entry_fee"]), "net_entry_cost": dec(ef["net_entry_cost"]), "strategy_id": strategy_id, "signal_id": ef["signal_id"], "event_key": ef["event_key"]}

    for xf in conn.execute("SELECT * FROM exit_fills WHERE mode=? ORDER BY filled_at_utc,row_id", (mode,)):
        allocs = conn.execute("SELECT * FROM exit_fill_allocations WHERE exit_fill_id=? AND mode=? ORDER BY row_id", (xf["exit_fill_id"], mode)).fetchall()
        if not allocs and dec(xf["filled_shares"]) > EPS:
            inc(checks, "EXIT_ALLOCATION_SHARES_MISMATCH")
            continue
        sums = {"shares": ZERO, "gross": ZERO, "fee": ZERO, "net": ZERO}
        for idx, alloc in enumerate(allocs, start=1):
            sums["shares"] += dec(alloc["allocated_shares"])
            sums["gross"] += dec(alloc["gross_exit_proceeds"])
            sums["fee"] += dec(alloc["exit_fee"])
            sums["net"] += dec(alloc["net_exit_proceeds"])
            if alloc["lot_id"] not in expected_lots:
                inc(checks, "EXIT_ALLOCATION_ORDER_MISMATCH")
        if not same_decimal(sums["shares"], xf["filled_shares"]):
            inc(checks, "EXIT_ALLOCATION_SHARES_MISMATCH")
        if not same_decimal(sums["gross"], xf["gross_exit_proceeds"]):
            inc(checks, "EXIT_ALLOCATION_GROSS_PROCEEDS_MISMATCH")
        if not same_decimal(sums["fee"], xf["exit_fee"]):
            inc(checks, "EXIT_ALLOCATION_FEE_MISMATCH")
        if not same_decimal(sums["net"], xf["net_exit_proceeds"]):
            inc(checks, "EXIT_ALLOCATION_NET_PROCEEDS_MISMATCH")

    for st in conn.execute("SELECT * FROM settlements WHERE mode=?", (mode,)):
        allocs = conn.execute("SELECT * FROM settlement_allocations WHERE settlement_id=? AND mode=? ORDER BY row_id", (st["settlement_id"], mode)).fetchall()
        sums = {"shares": ZERO, "gross": ZERO, "fee": ZERO, "net": ZERO}
        for alloc in allocs:
            sums["shares"] += dec(alloc["settled_shares"])
            sums["gross"] += dec(alloc["gross_settlement_proceeds"])
            sums["fee"] += dec(alloc["settlement_fee"])
            sums["net"] += dec(alloc["net_settlement_proceeds"])
            expected_value = dec(st["settlement_value"])
            if abs(dec(alloc["gross_settlement_proceeds"]) - dec(alloc["settled_shares"]) * expected_value) > Decimal("0.00001"):
                inc(checks, "SETTLEMENT_ALLOCATION_VALUE_MISMATCH")
        if not same_decimal(sums["shares"], st["remaining_shares_settled"]):
            inc(checks, "SETTLEMENT_ALLOCATION_SHARES_MISMATCH")
        if not same_decimal(sums["net"], st["net_settlement_proceeds"]):
            inc(checks, "SETTLEMENT_ALLOCATION_NET_PROCEEDS_MISMATCH")

    for lot in conn.execute("SELECT * FROM strategy_lots WHERE mode=?", (mode,)):
        sold = dec(conn.execute("SELECT COALESCE(SUM(CAST(allocated_shares AS REAL)),0) v FROM exit_fill_allocations WHERE lot_id=? AND mode=?", (lot["lot_id"], mode)).fetchone()["v"])
        gross_exit = dec(conn.execute("SELECT COALESCE(SUM(CAST(gross_exit_proceeds AS REAL)),0) v FROM exit_fill_allocations WHERE lot_id=? AND mode=?", (lot["lot_id"], mode)).fetchone()["v"])
        net_exit = dec(conn.execute("SELECT COALESCE(SUM(CAST(net_exit_proceeds AS REAL)),0) v FROM exit_fill_allocations WHERE lot_id=? AND mode=?", (lot["lot_id"], mode)).fetchone()["v"])
        settled = dec(conn.execute("SELECT COALESCE(SUM(CAST(settled_shares AS REAL)),0) v FROM settlement_allocations WHERE lot_id=? AND mode=?", (lot["lot_id"], mode)).fetchone()["v"])
        gross_settlement = dec(conn.execute("SELECT COALESCE(SUM(CAST(gross_settlement_proceeds AS REAL)),0) v FROM settlement_allocations WHERE lot_id=? AND mode=?", (lot["lot_id"], mode)).fetchone()["v"])
        net_settlement = dec(conn.execute("SELECT COALESCE(SUM(CAST(net_settlement_proceeds AS REAL)),0) v FROM settlement_allocations WHERE lot_id=? AND mode=?", (lot["lot_id"], mode)).fetchone()["v"])
        remaining = max(dec(lot["entry_shares"]) - sold - settled, ZERO)
        gross_pnl = gross_exit + gross_settlement - dec(lot["gross_entry_cost"])
        net_pnl = net_exit + net_settlement - dec(lot["net_entry_cost"])
        if not same_decimal(lot["sold_shares"], sold):
            inc(checks, "LOT_SOLD_SHARES_MISMATCH")
        if not same_decimal(lot["remaining_shares"], remaining):
            inc(checks, "LOT_REMAINING_SHARES_MISMATCH")
        if not same_decimal(lot["gross_exit_proceeds"], gross_exit) or not same_decimal(lot["net_exit_proceeds"], net_exit):
            inc(checks, "LOT_EXIT_PROCEEDS_MISMATCH")
        if not same_decimal(lot["gross_settlement_proceeds"], gross_settlement) or not same_decimal(lot["net_settlement_proceeds"], net_settlement):
            inc(checks, "LOT_SETTLEMENT_PROCEEDS_MISMATCH")
        if not same_decimal(lot["gross_pnl"], gross_pnl) or not same_decimal(lot["net_pnl"], net_pnl):
            inc(checks, "LOT_PNL_MISMATCH")
        if abs((sold + settled + remaining) - dec(lot["entry_shares"])) > Decimal("0.00001"):
            inc(checks, "POSITION_SHARE_CONSERVATION_FAILED")

    expected_events: dict[tuple[str, str], dict[str, Decimal]] = {}
    for event_key in [r["event_key"] for r in conn.execute("SELECT DISTINCT event_key FROM signals WHERE mode=?", (mode,))]:
        for strategy_id in STRATEGY_IDS:
            gross_entry = dec(conn.execute("SELECT COALESCE(SUM(CAST(gross_entry_cost AS REAL)),0) v FROM strategy_lots WHERE event_key=? AND strategy_id=? AND mode=?", (event_key, strategy_id, mode)).fetchone()["v"])
            entry_fee = dec(conn.execute("SELECT COALESCE(SUM(CAST(entry_fee AS REAL)),0) v FROM strategy_lots WHERE event_key=? AND strategy_id=? AND mode=?", (event_key, strategy_id, mode)).fetchone()["v"])
            gross_exit = dec(conn.execute("SELECT COALESCE(SUM(CAST(gross_exit_proceeds AS REAL)),0) v FROM exit_fill_allocations WHERE event_key=? AND strategy_id=? AND mode=?", (event_key, strategy_id, mode)).fetchone()["v"])
            exit_fee = dec(conn.execute("SELECT COALESCE(SUM(CAST(exit_fee AS REAL)),0) v FROM exit_fill_allocations WHERE event_key=? AND strategy_id=? AND mode=?", (event_key, strategy_id, mode)).fetchone()["v"])
            gross_settlement = dec(conn.execute("SELECT COALESCE(SUM(CAST(gross_settlement_proceeds AS REAL)),0) v FROM settlement_allocations WHERE event_key=? AND strategy_id=? AND mode=?", (event_key, strategy_id, mode)).fetchone()["v"])
            settlement_fee = dec(conn.execute("SELECT COALESCE(SUM(CAST(settlement_fee AS REAL)),0) v FROM settlement_allocations WHERE event_key=? AND strategy_id=? AND mode=?", (event_key, strategy_id, mode)).fetchone()["v"])
            total_fees = entry_fee + exit_fee + settlement_fee
            gross_pnl = gross_exit + gross_settlement - gross_entry
            net_pnl = gross_pnl - total_fees
            expected_events[(event_key, strategy_id)] = {"gross_entry_cost": gross_entry, "entry_fee": entry_fee, "gross_exit_proceeds": gross_exit, "exit_fee": exit_fee, "gross_settlement_proceeds": gross_settlement, "settlement_fee": settlement_fee, "total_fees": total_fees, "gross_pnl": gross_pnl, "net_pnl": net_pnl}
            er = conn.execute("SELECT * FROM event_results WHERE event_key=? AND strategy_id=? AND mode=? ORDER BY row_id DESC LIMIT 1", (event_key, strategy_id, mode)).fetchone()
            if er and er["net_pnl"] is not None and (not same_decimal(er["net_pnl"], net_pnl) or not same_decimal(er["gross_pnl"], gross_pnl)):
                inc(checks, "EVENT_PNL_MISMATCH")
            if er:
                expected_incomplete = int(conn.execute(
                    """
                    SELECT 1 FROM strategy_triggers st
                    JOIN (SELECT trigger_id,mode,MAX(row_id) row_id FROM strategy_triggers GROUP BY trigger_id,mode) latest
                      ON latest.row_id=st.row_id
                    WHERE st.event_key=? AND st.strategy_id=? AND st.mode=? AND st.trigger_status='open'
                    LIMIT 1
                    """,
                    (event_key, strategy_id, mode),
                ).fetchone() is not None)
                if int(er["incomplete_take_profit"]) != expected_incomplete:
                    inc(checks, "INCOMPLETE_TAKE_PROFIT_MISMATCH")

    for strategy_id in STRATEGY_IDS:
        sums = {k: sum((v[k] for (event, st), v in expected_events.items() if st == strategy_id), ZERO) for k in ["gross_entry_cost", "entry_fee", "gross_exit_proceeds", "exit_fee", "gross_settlement_proceeds", "settlement_fee", "total_fees", "gross_pnl", "net_pnl"]}
        st_row = conn.execute("SELECT * FROM strategy_totals WHERE strategy_id=? AND mode=?", (strategy_id, mode)).fetchone()
        if st_row and any(not same_decimal(st_row[k], sums[k]) for k in sums):
            inc(checks, "STRATEGY_PNL_MISMATCH")
    ledger_sums = {k: ZERO for k in ["gross_entry_cost", "entry_fee", "gross_exit_proceeds", "exit_fee", "gross_settlement_proceeds", "settlement_fee", "total_fees", "gross_pnl", "net_pnl"]}
    for values in expected_events.values():
        for key in ledger_sums:
            ledger_sums[key] += values[key]
    ledger_row = conn.execute("SELECT * FROM ledger_totals WHERE mode=?", (mode,)).fetchone()
    if ledger_row and any(not same_decimal(ledger_row[k], ledger_sums[k]) for k in ledger_sums):
        inc(checks, "TOTAL_LEDGER_PNL_MISMATCH")


def audit_positions_and_pnl(conn: sqlite3.Connection, mode: str, checks: dict[str, Any]) -> None:
    for code in ["POSITION_SHARE_CONSERVATION_FAILED", "POSITION_COST_BASIS_MISMATCH", "POSITION_PNL_MISMATCH", "TRIGGER_TARGET_EXCEEDED", "TRIGGER_STATUS_MISMATCH", "SIGNAL_TOKEN_PNL_MISMATCH", "EVENT_PNL_MISMATCH", "TOTAL_LEDGER_PNL_MISMATCH"]:
        checks.setdefault(code, 0)
    for sid in [r["signal_id"] for r in conn.execute("SELECT DISTINCT signal_id FROM signals WHERE mode=?", (mode,))]:
        for strategy_id in STRATEGY_IDS:
            bought = dec(conn.execute("SELECT COALESCE(SUM(CAST(entry_shares AS REAL)),0) v FROM strategy_lots WHERE signal_id=? AND strategy_id=? AND mode=?", (sid, strategy_id, mode)).fetchone()["v"])
            sold = dec(conn.execute("SELECT COALESCE(SUM(CAST(allocated_shares AS REAL)),0) v FROM exit_fill_allocations WHERE signal_id=? AND strategy_id=? AND mode=?", (sid, strategy_id, mode)).fetchone()["v"])
            settled = dec(conn.execute("SELECT COALESCE(SUM(CAST(settled_shares AS REAL)),0) v FROM settlement_allocations WHERE signal_id=? AND strategy_id=? AND mode=?", (sid, strategy_id, mode)).fetchone()["v"])
            if sold + settled - bought > EPS:
                inc(checks, "POSITION_SHARE_CONSERVATION_FAILED")
            lot_cost = dec(conn.execute("SELECT COALESCE(SUM(CAST(net_entry_cost AS REAL)),0) v FROM strategy_lots WHERE signal_id=? AND strategy_id=? AND mode=?", (sid, strategy_id, mode)).fetchone()["v"])
            entry_cost = dec(conn.execute("SELECT COALESCE(SUM(CAST(net_entry_cost AS REAL)),0) v FROM entry_fills WHERE signal_id=? AND mode=?", (sid, mode)).fetchone()["v"])
            if bought > ZERO and abs(lot_cost - entry_cost) > Decimal("0.00001"):
                inc(checks, "POSITION_COST_BASIS_MISMATCH")
    for row in conn.execute(
        """
        SELECT latest.*, COALESCE(SUM(CAST(xf.filled_shares AS REAL)),0) exit_sum
        FROM (
          SELECT st.* FROM strategy_triggers st
          JOIN (SELECT trigger_id,mode,MAX(row_id) row_id FROM strategy_triggers GROUP BY trigger_id,mode) lr ON st.row_id=lr.row_id
        ) latest
        LEFT JOIN exit_fills xf ON latest.trigger_id=xf.trigger_id AND latest.mode=xf.mode
        WHERE latest.mode=?
        GROUP BY latest.trigger_id, latest.mode
        """,
        (mode,),
    ):
        target = dec(row["trigger_target_shares"])
        filled = dec(row["trigger_filled_shares"])
        exit_sum = dec(row["exit_sum"])
        if filled - target > EPS or exit_sum - target > EPS:
            inc(checks, "TRIGGER_TARGET_EXCEEDED")
        expected_remaining = max(target - filled, ZERO)
        expected_status = "completed" if expected_remaining <= EPS else "open"
        if not same_decimal(row["trigger_remaining_shares"], expected_remaining) or row["trigger_status"] != expected_status:
            inc(checks, "TRIGGER_STATUS_MISMATCH")
    unsettled_lots = conn.execute(
        """
        SELECT COUNT(*) c
        FROM strategy_lots sl
        LEFT JOIN settlements s ON sl.signal_id=s.signal_id AND sl.strategy_id=s.strategy_id AND sl.mode=s.mode
        WHERE sl.mode=? AND s.row_id IS NULL
        """,
        (mode,),
    ).fetchone()["c"]
    if unsettled_lots == 0:
        total_event_net = dec(conn.execute("SELECT COALESCE(SUM(CAST(net_pnl AS REAL)),0) v FROM event_results WHERE mode=?", (mode,)).fetchone()["v"])
        total_entry = dec(conn.execute("SELECT COALESCE(SUM(CAST(gross_entry_cost AS REAL)),0) v FROM strategy_lots WHERE mode=?", (mode,)).fetchone()["v"])
        total_entry_fee = dec(conn.execute("SELECT COALESCE(SUM(CAST(entry_fee AS REAL)),0) v FROM strategy_lots WHERE mode=?", (mode,)).fetchone()["v"])
        total_exit = dec(conn.execute("SELECT COALESCE(SUM(CAST(gross_exit_proceeds AS REAL)),0) v FROM exit_fills WHERE mode=?", (mode,)).fetchone()["v"])
        total_exit_fee = dec(conn.execute("SELECT COALESCE(SUM(CAST(exit_fee AS REAL)),0) v FROM exit_fills WHERE mode=?", (mode,)).fetchone()["v"])
        total_settlement = dec(conn.execute("SELECT COALESCE(SUM(CAST(gross_settlement_proceeds AS REAL)),0) v FROM settlements WHERE mode=?", (mode,)).fetchone()["v"])
        total_settlement_fee = dec(conn.execute("SELECT COALESCE(SUM(CAST(settlement_fee AS REAL)),0) v FROM settlements WHERE mode=?", (mode,)).fetchone()["v"])
        expected_total = total_exit + total_settlement - total_entry - total_entry_fee - total_exit_fee - total_settlement_fee
        if abs(total_event_net - expected_total) > Decimal("0.00001"):
            inc(checks, "TOTAL_LEDGER_PNL_MISMATCH")


def monitor_once(root: Path, mode: str, config_path: Path, run_id: str | None = None, adapter: PublicAdapter | None = None, now: datetime | None = None, recover_stale_lock: bool = False) -> dict[str, Any]:
    config = load_config(config_path)
    now = (now or utcnow()).astimezone(timezone.utc)
    rid = run_id or make_run_id("monitor_once", now)
    if mode == FORMAL and adapter is not None:
        db = init_ledger(root, mode, config_path)
        conn = connect(db)
        try:
            with conn:
                append_audit(conn, mode, rid, "formal_adapter_injection_rejected", {"adapter_class": adapter.__class__.__name__}, "error", now)
            return {"run_id": rid, "fatal_error": True, "status": "formal_adapter_injection_rejected"}
        finally:
            conn.close()
    hashes = current_hashes(root, config_path)
    with acquire_monitor_lock(root, mode, config, rid, command="monitor_once", recover_stale_lock=recover_stale_lock, now=now, config_hash=hashes.get("config_sha256", ""), code_hash=hashes.get("core_code_sha256", "")) as lock_token:
        return _monitor_once_under_lock(root, mode, config_path, rid, lock_token, adapter=adapter, now=now, command="monitor_once")


def _monitor_once_under_lock(root: Path, mode: str, config_path: Path, run_id: str, lock_token: dict[str, Any] | None, adapter: PublicAdapter | None = None, now: datetime | None = None, command: str = "monitor_once") -> dict[str, Any]:
    config = load_config(config_path)
    now = (now or utcnow()).astimezone(timezone.utc)
    validate_lock_token(root, mode, config, run_id, lock_token)
    lock_id = str(lock_token.get("lock_id") or "")
    db = init_ledger(root, mode, config_path)
    adapter = build_public_adapter(config, mode) if mode == FORMAL else (adapter or build_public_adapter(config, mode))
    provider = PublicMarketProvider(adapter)
    conn = connect(db)
    results: list[dict[str, Any]] = []
    selected_tokens: set[str] = set()
    try:
        assert_formal_hashes(root, mode, config_path, conn)
        with conn:
            rid = create_run(conn, mode, command, current_hashes(root, config_path).get("config_sha256", ""), current_hashes(root, config_path), run_id, now, lock_id=lock_id)
            if get_state(conn, "paused", "false") == "true":
                append_audit(conn, mode, rid, "monitor_paused", {"run_id": rid}, "info", now)
                finalize_run(conn, rid, [], {"adapter_version": ADAPTER_VERSION, "results": [], "paused": True}, now)
                return {"run_id": rid, "results": [], "paused": True}
            if get_state(conn, "stopped", "false") == "true":
                append_audit(conn, mode, rid, "monitor_stopped", {"run_id": rid}, "info", now)
                finalize_run(conn, rid, [], {"adapter_version": ADAPTER_VERSION, "results": [], "stopped": True}, now)
                return {"run_id": rid, "results": [], "stopped": True}
            signals = {r["signal_id"]: r for r in active_signals(conn, mode)}
            for sid in open_signal_ids(conn, mode):
                signals.setdefault(sid, get_signal(conn, sid, mode))
            active_by_token: dict[str, list[tuple[sqlite3.Row, Any, Any]]] = {}
            for sig in sorted(signals.values(), key=lambda r: (r["created_at_utc"], r["signal_id"])):
                selected_tokens.add(sig["token_id"])
                try:
                    gamma_result, clob_result = provider.market_bundle_without_book(sig)
                    state = market_state(gamma_result.payload, clob_result.payload)
                    append_audit(conn, mode, rid, "market_state_observed", {"signal_id": sig["signal_id"], "market_slug": sig["market_slug"], **state}, "info", now)
                    if state["market_status"] == "active_trading":
                        active_by_token.setdefault(sig["token_id"], []).append((sig, gamma_result, clob_result))
                    elif state["market_status"] in {"resolution_pending", "active_not_accepting_orders", "active_accepting_orders_unknown", "status_conflict", "disputed", "unknown"}:
                        results.append({"signal_id": sig["signal_id"], "status": state["market_status"], "entry_exit_blocked": True})
                    elif state["market_status"] == "resolved":
                        clob_public_result = provider.clob_public_market(sig["condition_id"])
                        gamma_eid = record_http_evidence(conn, mode, "gamma_market", rid, sig["signal_id"], sig["token_id"], sig["condition_id"], sig["market_slug"], gamma_result)
                        clob_eid = record_http_evidence(conn, mode, "clob_market", rid, sig["signal_id"], sig["token_id"], sig["condition_id"], sig["market_slug"], clob_result)
                        clob_public_eid = record_http_evidence(conn, mode, "clob_public_market", rid, sig["signal_id"], sig["token_id"], sig["condition_id"], sig["market_slug"], clob_public_result)
                        write_resolved_raw_evidence(root, config, mode, rid, sig, gamma_result, clob_result, now, clob_public_result)
                        results.extend(settle_signal_with_market(conn, rid, mode, sig, gamma_result.payload, clob_result.payload, gamma_result.url, now, root, config_path, clob_public_result.payload, lock_id=lock_id, gamma_market_evidence_id=gamma_eid, clob_market_evidence_id=clob_eid, clob_public_market_evidence_id=clob_public_eid))
                    else:
                        results.append({"signal_id": sig["signal_id"], "status": "unknown_state", "entry_exit_blocked": True})
                except Exception as exc:
                    append_audit(conn, mode, rid, "monitor_market_error", {"signal_id": sig["signal_id"], "error": str(exc)}, "error", now)
                    results.append({"signal_id": sig["signal_id"], "status": "error", "error": str(exc)})
            for token_id, bundle_rows in active_by_token.items():
                try:
                    book_result = provider.orderbook(token_id)
                    results.extend(process_active_token_batch(conn, rid, mode, token_id, bundle_rows, book_result, now, root, config_path, lock_id=lock_id))
                except Exception as exc:
                    append_audit(conn, mode, rid, "token_batch_error", {"token_id": token_id, "error": str(exc)}, "error", now)
                    results.append({"token_id": token_id, "status": "error", "error": str(exc)})
            aggregate_results_conn(conn, mode)
            finalize_run(conn, rid, sorted(selected_tokens), {"adapter_version": ADAPTER_VERSION, "results": results}, now)
        return {"run_id": rid, "results": json_safe(results)}
    finally:
        conn.close()


def monitor_control(root: Path, mode: str, config_path: Path, action: str, now: datetime | None = None) -> dict[str, Any]:
    if action not in {"pause", "resume", "stop"}:
        raise ValueError("action must be pause, resume, or stop")
    config = load_config(config_path)
    db = init_ledger(root, mode, config_path)
    conn = connect(db)
    try:
        assert_formal_hashes(root, mode, config_path, conn)
        with conn:
            if action == "pause":
                set_state(conn, "paused", "true")
                set_state(conn, "stopped", "false")
            elif action == "resume":
                set_state(conn, "paused", "false")
                set_state(conn, "stopped", "false")
            else:
                set_state(conn, "stopped", "true")
            append_audit(conn, mode, "", f"monitor_{action}_requested", {"action": action}, "info", now)
        return {"mode": mode, "action": action, "paused": get_state(conn, "paused", "false"), "stopped": get_state(conn, "stopped", "false")}
    finally:
        conn.close()


def run_loop(
    root: Path,
    mode: str,
    config_path: Path,
    iterations: int,
    interval_seconds: Decimal,
    run_id: str | None = None,
    confirm_infinite: bool = False,
    recover_stale_lock: bool = False,
    sleep_func: Any = time.sleep,
) -> dict[str, Any]:
    if iterations <= 0 and not confirm_infinite:
        raise RuntimeError("iterations=0 is an infinite loop and requires --confirm-infinite")
    config = load_config(config_path)
    completed = 0
    rid = run_id or make_run_id("run_loop")
    interrupted = False
    pause_poll = Decimal(str(config.get("execution", {}).get("pause_poll_seconds", 5)))
    backoff = Decimal(str(config.get("polling", {}).get("retry_backoff_seconds", 2)))
    hashes = current_hashes(root, config_path)
    with acquire_monitor_lock(root, mode, config, rid, command="run_loop", recover_stale_lock=recover_stale_lock, config_hash=hashes.get("config_sha256", ""), code_hash=hashes.get("core_code_sha256", "")) as lock_token:
        try:
            while iterations == 0 or completed < iterations:
                flags = read_monitor_flags(root, mode, config_path)
                if flags["stopped"] == "true":
                    write_monitor_heartbeat(root, mode, config, rid, "stopped")
                    break
                if flags["paused"] == "true":
                    write_monitor_heartbeat(root, mode, config, rid, "paused")
                    sleep_func(float(pause_poll))
                    continue
                write_monitor_heartbeat(root, mode, config, rid, "running")
                result = _monitor_once_under_lock(root, mode, config_path, rid, lock_token, now=utcnow(), command="run_loop")
                if result.get("fatal_error"):
                    conn = connect(db_path(root, mode, config))
                    try:
                        with conn:
                            set_state(conn, "stopped", "true")
                            append_audit(conn, mode, rid, "run_loop_fatal_error", {"result": result}, "error")
                    finally:
                        conn.close()
                    write_monitor_heartbeat(root, mode, config, rid, "failed")
                    break
                completed += 1
                flags = read_monitor_flags(root, mode, config_path)
                if flags["stopped"] == "true" or result.get("stopped"):
                    write_monitor_heartbeat(root, mode, config, rid, "stopped")
                    break
                if iterations > 0 and completed >= iterations:
                    write_monitor_heartbeat(root, mode, config, rid, "completed")
                    break
                if result.get("recoverable_error"):
                    write_monitor_heartbeat(root, mode, config, rid, "recovering")
                    sleep_func(float(backoff))
                elif interval_seconds > ZERO:
                    write_monitor_heartbeat(root, mode, config, rid, "running")
                    sleep_func(float(interval_seconds))
        except KeyboardInterrupt:
            interrupted = True
            conn = connect(db_path(root, mode, config))
            try:
                with conn:
                    set_state(conn, "stopped", "true")
                    set_state(conn, "run_status", "stopped_by_user")
                    append_audit(conn, mode, rid, "run_loop_keyboard_interrupt", {"iterations_completed": completed}, "warning")
            finally:
                conn.close()
            write_monitor_heartbeat(root, mode, config, rid, "stopped_by_user")
    return {"run_id": rid, "iterations_completed": completed, "interrupted": interrupted}


def refresh_strategy_lot_cache(conn: sqlite3.Connection, mode: str) -> None:
    for lot in conn.execute("SELECT * FROM strategy_lots WHERE mode=?", (mode,)):
        sold = dec(conn.execute("SELECT COALESCE(SUM(CAST(allocated_shares AS REAL)),0) v FROM exit_fill_allocations WHERE lot_id=? AND mode=?", (lot["lot_id"], mode)).fetchone()["v"])
        gross_exit = dec(conn.execute("SELECT COALESCE(SUM(CAST(gross_exit_proceeds AS REAL)),0) v FROM exit_fill_allocations WHERE lot_id=? AND mode=?", (lot["lot_id"], mode)).fetchone()["v"])
        exit_fee = dec(conn.execute("SELECT COALESCE(SUM(CAST(exit_fee AS REAL)),0) v FROM exit_fill_allocations WHERE lot_id=? AND mode=?", (lot["lot_id"], mode)).fetchone()["v"])
        net_exit = dec(conn.execute("SELECT COALESCE(SUM(CAST(net_exit_proceeds AS REAL)),0) v FROM exit_fill_allocations WHERE lot_id=? AND mode=?", (lot["lot_id"], mode)).fetchone()["v"])
        settled = dec(conn.execute("SELECT COALESCE(SUM(CAST(settled_shares AS REAL)),0) v FROM settlement_allocations WHERE lot_id=? AND mode=?", (lot["lot_id"], mode)).fetchone()["v"])
        gross_settlement = dec(conn.execute("SELECT COALESCE(SUM(CAST(gross_settlement_proceeds AS REAL)),0) v FROM settlement_allocations WHERE lot_id=? AND mode=?", (lot["lot_id"], mode)).fetchone()["v"])
        settlement_fee = dec(conn.execute("SELECT COALESCE(SUM(CAST(settlement_fee AS REAL)),0) v FROM settlement_allocations WHERE lot_id=? AND mode=?", (lot["lot_id"], mode)).fetchone()["v"])
        net_settlement = dec(conn.execute("SELECT COALESCE(SUM(CAST(net_settlement_proceeds AS REAL)),0) v FROM settlement_allocations WHERE lot_id=? AND mode=?", (lot["lot_id"], mode)).fetchone()["v"])
        remaining = max(dec(lot["entry_shares"]) - sold - settled, ZERO)
        gross_pnl = gross_exit + gross_settlement - dec(lot["gross_entry_cost"])
        net_pnl = net_exit + net_settlement - dec(lot["net_entry_cost"])
        conn.execute(
            """
            UPDATE strategy_lots
            SET sold_shares=?,gross_exit_proceeds=?,exit_fee=?,net_exit_proceeds=?,
                settled_shares=?,gross_settlement_proceeds=?,settlement_fee=?,net_settlement_proceeds=?,
                remaining_shares=?,gross_pnl=?,net_pnl=?
            WHERE lot_id=? AND mode=?
            """,
            (dstr(sold), dstr(gross_exit), dstr(exit_fee), dstr(net_exit), dstr(settled), dstr(gross_settlement), dstr(settlement_fee), dstr(net_settlement), dstr(remaining), dstr(gross_pnl), dstr(net_pnl), lot["lot_id"], mode),
        )


def aggregate_results_conn(conn: sqlite3.Connection, mode: str) -> list[dict[str, Any]]:
    refresh_strategy_lot_cache(conn, mode)
    conn.execute("DELETE FROM event_results WHERE mode=?", (mode,))
    conn.execute("DELETE FROM strategy_totals WHERE mode=?", (mode,))
    conn.execute("DELETE FROM ledger_totals WHERE mode=?", (mode,))
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
                "incomplete_take_profit": int(conn.execute(
                    """
                    SELECT 1
                    FROM strategy_triggers st
                    JOIN (SELECT trigger_id,mode,MAX(row_id) row_id FROM strategy_triggers GROUP BY trigger_id,mode) latest
                      ON latest.row_id=st.row_id
                    WHERE st.event_key=? AND st.strategy_id=? AND st.mode=? AND st.trigger_status='open'
                    LIMIT 1
                    """,
                    (event_key, strategy_id, mode),
                ).fetchone() is not None),
            }
            conn.execute(
                "INSERT INTO event_results(event_key,strategy_id,mode,signal_count,position_count,traded_event_count,settled_event_count,gross_entry_cost,entry_fee,gross_exit_proceeds,exit_fee,gross_settlement_proceeds,settlement_fee,total_fees,gross_pnl,net_pnl,triggered_take_profit,incomplete_take_profit) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (event_key, strategy_id, mode, row["signal_count"], row["position_count"], row["traded_event_count"], row["settled_event_count"], dstr(gross_entry), dstr(entry_fee), dstr(gross_exit), dstr(exit_fee), dstr(gross_settlement), dstr(settlement_fee), dstr(total_fees), None if row["gross_pnl"] is None else dstr(row["gross_pnl"]), None if row["net_pnl"] is None else dstr(row["net_pnl"]), row["triggered_take_profit"], row["incomplete_take_profit"]),
            )
            rows.append(row)
    ledger = {"gross_entry_cost": ZERO, "entry_fee": ZERO, "gross_exit_proceeds": ZERO, "exit_fee": ZERO, "gross_settlement_proceeds": ZERO, "settlement_fee": ZERO, "total_fees": ZERO, "gross_pnl": ZERO, "net_pnl": ZERO}
    for strategy_id in STRATEGY_IDS:
        gross_entry = dec(conn.execute("SELECT COALESCE(SUM(CAST(gross_entry_cost AS REAL)),0) v FROM strategy_lots WHERE strategy_id=? AND mode=?", (strategy_id, mode)).fetchone()["v"])
        entry_fee = dec(conn.execute("SELECT COALESCE(SUM(CAST(entry_fee AS REAL)),0) v FROM strategy_lots WHERE strategy_id=? AND mode=?", (strategy_id, mode)).fetchone()["v"])
        gross_exit = dec(conn.execute("SELECT COALESCE(SUM(CAST(gross_exit_proceeds AS REAL)),0) v FROM exit_fill_allocations WHERE strategy_id=? AND mode=?", (strategy_id, mode)).fetchone()["v"])
        exit_fee = dec(conn.execute("SELECT COALESCE(SUM(CAST(exit_fee AS REAL)),0) v FROM exit_fill_allocations WHERE strategy_id=? AND mode=?", (strategy_id, mode)).fetchone()["v"])
        gross_settlement = dec(conn.execute("SELECT COALESCE(SUM(CAST(gross_settlement_proceeds AS REAL)),0) v FROM settlement_allocations WHERE strategy_id=? AND mode=?", (strategy_id, mode)).fetchone()["v"])
        settlement_fee = dec(conn.execute("SELECT COALESCE(SUM(CAST(settlement_fee AS REAL)),0) v FROM settlement_allocations WHERE strategy_id=? AND mode=?", (strategy_id, mode)).fetchone()["v"])
        total_fees = entry_fee + exit_fee + settlement_fee
        gross_pnl = gross_exit + gross_settlement - gross_entry
        net_pnl = gross_pnl - total_fees
        conn.execute(
            "INSERT OR REPLACE INTO strategy_totals(strategy_id,mode,gross_entry_cost,entry_fee,gross_exit_proceeds,exit_fee,gross_settlement_proceeds,settlement_fee,total_fees,gross_pnl,net_pnl) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (strategy_id, mode, dstr(gross_entry), dstr(entry_fee), dstr(gross_exit), dstr(exit_fee), dstr(gross_settlement), dstr(settlement_fee), dstr(total_fees), dstr(gross_pnl), dstr(net_pnl)),
        )
        for key, value in [("gross_entry_cost", gross_entry), ("entry_fee", entry_fee), ("gross_exit_proceeds", gross_exit), ("exit_fee", exit_fee), ("gross_settlement_proceeds", gross_settlement), ("settlement_fee", settlement_fee), ("total_fees", total_fees), ("gross_pnl", gross_pnl), ("net_pnl", net_pnl)]:
            ledger[key] += value
    conn.execute(
        "INSERT OR REPLACE INTO ledger_totals(mode,gross_entry_cost,entry_fee,gross_exit_proceeds,exit_fee,gross_settlement_proceeds,settlement_fee,total_fees,gross_pnl,net_pnl) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (mode, dstr(ledger["gross_entry_cost"]), dstr(ledger["entry_fee"]), dstr(ledger["gross_exit_proceeds"]), dstr(ledger["exit_fee"]), dstr(ledger["gross_settlement_proceeds"]), dstr(ledger["settlement_fee"]), dstr(ledger["total_fees"]), dstr(ledger["gross_pnl"]), dstr(ledger["net_pnl"])),
    )
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
    lock_info = read_json_if_exists(lock_path(root, mode, config))
    heartbeat_info = read_json_if_exists(heartbeat_path(root, mode, config))
    if mode == FORMAL and not db.exists():
        return sanitize_status_for_release(
            root,
            {
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
                "lock_held": bool(lock_info),
                "lock_info": lock_info,
                "heartbeat": heartbeat_info,
            },
        )
    db = init_ledger(root, mode, config_path)
    conn = connect(db)
    try:
        return sanitize_status_for_release(
            root,
            {
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
                "paused": get_state(conn, "paused", "false"),
                "stopped": get_state(conn, "stopped", "false"),
                "lock_held": bool(lock_info),
                "lock_info": lock_info,
                "heartbeat": heartbeat_info,
            },
        )
    finally:
        conn.close()


def audit_integrity(root: Path, mode: str, config_path: Path, level: str = "full-replay") -> dict[str, Any]:
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
        checks["duplicate_snapshot_content_in_run"] = conn.execute(
            "SELECT COUNT(*) c FROM (SELECT run_id,token_id,content_hash,COUNT(*) n FROM orderbook_snapshots WHERE mode=? GROUP BY run_id,token_id,content_hash HAVING n>1)",
            (mode,),
        ).fetchone()["c"]
        checks["duplicate_signal_id"] = conn.execute(
            "SELECT COUNT(*) c FROM (SELECT signal_id,COUNT(*) n FROM signals WHERE mode=? GROUP BY signal_id HAVING n>1)",
            (mode,),
        ).fetchone()["c"]
        checks["fee_conflicts_entered"] = conn.execute("SELECT COUNT(*) c FROM entry_fills ef JOIN fee_validations fv ON ef.run_id=fv.run_id AND ef.mode=fv.mode WHERE ef.mode=? AND fv.fee_crosscheck_status='conflict'", (mode,)).fetchone()["c"]
        checks["mapping_invalid_entered"] = conn.execute("SELECT COUNT(*) c FROM entry_fills ef JOIN token_validations tv ON ef.run_id=tv.run_id AND ef.signal_id=tv.signal_id WHERE ef.mode=? AND tv.mapping_valid=0", (mode,)).fetchone()["c"]
        checks["unsupported_fee_exponent_entered"] = conn.execute("SELECT COUNT(*) c FROM entry_fills ef JOIN fee_validations fv ON ef.run_id=fv.run_id AND ef.mode=fv.mode WHERE ef.mode=? AND fv.fee_crosscheck_status='unsupported_fee_exponent'", (mode,)).fetchone()["c"]
        checks["settled_after_exit"] = conn.execute(
            "SELECT COUNT(*) c FROM exit_fills e JOIN settlements s ON e.signal_id=s.signal_id AND e.strategy_id=s.strategy_id AND e.mode=s.mode WHERE e.mode=? AND e.filled_at_utc > s.recorded_at_utc",
            (mode,),
        ).fetchone()["c"]
        checks["entry_after_settlement"] = conn.execute(
            "SELECT COUNT(*) c FROM entry_fills e JOIN settlements s ON e.signal_id=s.signal_id AND e.mode=s.mode WHERE e.mode=? AND e.filled_at_utc > s.recorded_at_utc",
            (mode,),
        ).fetchone()["c"]
        expected_adapter_hash = get_state(conn, "adapter_code_sha256", "") or adapter_code_hash(root, config_path)
        checks["formal_non_official_adapter_rows"] = 0
        checks["formal_adapter_hash_mismatch"] = 0
        checks["formal_missing_source_fields"] = 0
        if mode == FORMAL:
            for table in ["orderbook_snapshots", "entry_fills", "exit_fills", "settlements"]:
                for row in conn.execute(f"SELECT adapter_name,adapter_code_hash,data_source,run_environment FROM {table} WHERE mode=?", (mode,)):
                    values = [row["adapter_name"], row["adapter_code_hash"], row["data_source"], row["run_environment"]]
                    if any(v in ("", None) for v in values):
                        checks["formal_missing_source_fields"] += 1
                    if row["adapter_name"] != ADAPTER_NAME or row["data_source"] != "polymarket_public_api" or row["run_environment"] != FORMAL:
                        checks["formal_non_official_adapter_rows"] += 1
                    if expected_adapter_hash and row["adapter_code_hash"] != expected_adapter_hash:
                        checks["formal_adapter_hash_mismatch"] += 1
        checks["constraint_conflict_entered"] = conn.execute("SELECT COUNT(*) c FROM entry_fills ef JOIN orderbook_snapshots ob ON ef.snapshot_id=ob.snapshot_id AND ef.mode=ob.mode WHERE ef.mode=? AND ob.constraint_crosscheck_status IN ('conflict','unknown','')", (mode,)).fetchone()["c"]
        checks["missing_constraints_for_fill"] = conn.execute("SELECT COUNT(*) c FROM entry_fills ef JOIN orderbook_snapshots ob ON ef.snapshot_id=ob.snapshot_id AND ef.mode=ob.mode WHERE ef.mode=? AND (ob.selected_tick_size='' OR ob.selected_min_order_size='')", (mode,)).fetchone()["c"]
        checks["audit_violation_events"] = conn.execute("SELECT COUNT(*) c FROM audit_log WHERE mode=? AND event_type LIKE '%violation%'", (mode,)).fetchone()["c"]
        checks["run_manifest_hash_mismatch"] = 1 if get_state(conn, "run_manifest_hash_mismatch", "false") == "true" else 0
        checks["illegal_tick_price_entered"] = 0
        checks["below_min_order_fill"] = 0
        for fill_table, share_col, price_col in [("entry_fills", "filled_shares", "entry_vwap"), ("exit_fills", "filled_shares", "exit_vwap")]:
            for row in conn.execute(f"SELECT f.{share_col} shares,f.{price_col} price,ob.selected_tick_size tick,ob.selected_min_order_size min_order FROM {fill_table} f JOIN orderbook_snapshots ob ON f.snapshot_id=ob.snapshot_id AND f.mode=ob.mode WHERE f.mode=?", (mode,)):
                if row["tick"] not in ("", None) and dec(row["price"]) / dec(row["tick"]) != (dec(row["price"]) / dec(row["tick"])).to_integral_value():
                    checks["illegal_tick_price_entered"] += 1
                if row["min_order"] not in ("", None) and dec(row["shares"]) < dec(row["min_order"]):
                    checks["below_min_order_fill"] += 1
        checks["proposed_final_settlement"] = conn.execute("SELECT COUNT(*) c FROM settlements WHERE mode=? AND finality_status='resolved_final' AND (lower(resolution_status) LIKE '%proposed%' OR lower(uma_status) LIKE '%proposed%')", (mode,)).fetchone()["c"]
        checks["SETTLEMENT_PROPOSED_MARKED_FINAL"] = checks["proposed_final_settlement"]
        checks["SETTLEMENT_PENDING_MARKED_FINAL"] = conn.execute("SELECT COUNT(*) c FROM settlements WHERE mode=? AND finality_status='resolved_final' AND (lower(resolution_status) LIKE '%pending%' OR lower(uma_status) LIKE '%pending%')", (mode,)).fetchone()["c"]
        checks["non_final_settlement"] = conn.execute("SELECT COUNT(*) c FROM settlements WHERE mode=? AND finality_status!='resolved_final'", (mode,)).fetchone()["c"]
        checks["future_signal_too_far"] = 0
        checks["signal_registration_delay_exceeded"] = 0
        checks["signal_before_formal_start"] = 0
        checks["signal_event_key_mismatch"] = 0
        checks["signal_temperature_bucket_unparseable"] = 0
        max_delay = int(config["sample_rules"].get("max_signal_registration_delay_seconds", 300))
        future = int(config["sample_rules"].get("allowed_future_skew_seconds", 30))
        formal_started = get_state(conn, "formal_started_at_utc", "")
        for row in conn.execute("SELECT * FROM signals WHERE mode=?", (mode,)):
            try:
                created = parse_utc(row["created_at_utc"])
                registered = parse_utc(row["registered_at_utc"])
            except Exception:
                checks["future_signal_too_far"] += 1
                continue
            delay = (registered - created).total_seconds()
            if -delay > future:
                checks["future_signal_too_far"] += 1
            if delay > max_delay:
                checks["signal_registration_delay_exceeded"] += 1
            if mode == FORMAL and formal_started and created < parse_utc(formal_started) - timedelta(microseconds=1):
                checks["signal_before_formal_start"] += 1
            if row["event_key"] != make_event_key(row["city"], row["weather_date_local"], row["weather_metric"]):
                checks["signal_event_key_mismatch"] += 1
            if not parse_temperature_bucket(str(row["temperature_bucket"] or "")):
                checks["signal_temperature_bucket_unparseable"] += 1
        checks["entry_shared_depth_overfill"] = 0
        checks["exit_shared_depth_overfill"] = 0
        for snap in conn.execute("SELECT * FROM orderbook_snapshots WHERE mode=?", (mode,)):
            try:
                raw = json.loads(snap["raw_orderbook_json"])
                book = normalize_orderbook(raw, snap["token_id"], snap["condition_id"])
            except Exception:
                continue
            entry_shares = dec(conn.execute("SELECT COALESCE(SUM(CAST(filled_shares AS REAL)),0) v FROM entry_fills WHERE mode=? AND run_id=? AND snapshot_id=?", (mode, snap["run_id"], snap["snapshot_id"])).fetchone()["v"])
            if entry_shares - dec(book["total_ask_shares"]) > EPS:
                checks["entry_shared_depth_overfill"] += 1
            for strategy_id in STRATEGY_IDS:
                exit_shares = dec(conn.execute("SELECT COALESCE(SUM(CAST(filled_shares AS REAL)),0) v FROM exit_fills WHERE mode=? AND run_id=? AND snapshot_id=? AND strategy_id=?", (mode, snap["run_id"], snap["snapshot_id"], strategy_id)).fetchone()["v"])
                if exit_shares - dec(book["total_bid_shares"]) > EPS:
                    checks["exit_shared_depth_overfill"] += 1
        checks["missing_settlement_raw_response"] = 0
        checks["settlement_raw_hash_mismatch"] = 0
        checks["SETTLEMENT_EVIDENCE_MISSING"] = 0
        checks["SETTLEMENT_GAMMA_HASH_MISMATCH"] = 0
        checks["SETTLEMENT_CLOB_HASH_MISMATCH"] = 0
        checks["SETTLEMENT_WINNER_MISMATCH"] = 0
        checks["SETTLEMENT_VALUE_MISMATCH"] = 0
        checks["SETTLEMENT_PROCEEDS_MISMATCH"] = 0
        checks["SETTLEMENT_FINALITY_CONFLICT"] = 0
        checks["SETTLEMENT_UNKNOWN_WINNER_ASSIGNED_VALUE"] = 0
        for row in conn.execute("SELECT * FROM settlements WHERE mode=?", (mode,)):
            raw_response = row["raw_response"]
            if not raw_response:
                checks["missing_settlement_raw_response"] += 1
                checks["SETTLEMENT_EVIDENCE_MISSING"] += 1
                continue
            try:
                gamma_payload = json.loads(raw_response)
                if content_hash(gamma_payload) != row["raw_response_hash"]:
                    checks["settlement_raw_hash_mismatch"] += 1
                    checks["SETTLEMENT_GAMMA_HASH_MISMATCH"] += 1
            except Exception:
                checks["settlement_raw_hash_mismatch"] += 1
                checks["SETTLEMENT_GAMMA_HASH_MISMATCH"] += 1
                continue
            raw_clob = row["raw_clob_response"]
            if not raw_clob:
                checks["SETTLEMENT_EVIDENCE_MISSING"] += 1
                continue
            try:
                clob_payload = json.loads(raw_clob)
                if content_hash(clob_payload) != row["raw_clob_response_hash"]:
                    checks["SETTLEMENT_CLOB_HASH_MISMATCH"] += 1
            except Exception:
                checks["SETTLEMENT_CLOB_HASH_MISMATCH"] += 1
                continue
            clob_market_payload = clob_payload.get("clob_markets") if isinstance(clob_payload, dict) else {}
            clob_public_payload = clob_payload.get("clob_public_market") if isinstance(clob_payload, dict) else {}
            pairs = clob_token_pairs(clob_public_payload or {}) or clob_token_pairs(clob_market_payload or {}) or gamma_token_pairs(gamma_payload)
            evidence = parse_settlement_evidence(gamma_payload, pairs)
            if not evidence.get("evidence_valid"):
                finality = str(evidence.get("finality_status") or evidence.get("settlement_status") or "")
                if "proposed" in finality:
                    checks["SETTLEMENT_PROPOSED_MARKED_FINAL"] += 1
                elif "pending" in finality or "not_final" in finality or "not_settleable" in finality:
                    checks["SETTLEMENT_PENDING_MARKED_FINAL"] += 1
                elif "winner" in str(evidence.get("error", "")) or "winner" in finality:
                    checks["SETTLEMENT_UNKNOWN_WINNER_ASSIGNED_VALUE"] += 1
                else:
                    checks["SETTLEMENT_FINALITY_CONFLICT"] += 1
                continue
            if row["finality_status"] != evidence.get("finality_status"):
                checks["SETTLEMENT_FINALITY_CONFLICT"] += 1
            if str(row["winning_asset_id"] or "") != str(evidence.get("winning_asset_id") or ""):
                checks["SETTLEMENT_WINNER_MISMATCH"] += 1
            if " ".join(str(row["winning_outcome"] or "").lower().split()) != " ".join(str(evidence.get("winning_outcome") or "").lower().split()):
                checks["SETTLEMENT_WINNER_MISMATCH"] += 1
            expected_values = evidence.get("token_settlement_values") or {}
            expected_value = expected_values.get(row["token_id"])
            if expected_value is None:
                checks["SETTLEMENT_UNKNOWN_WINNER_ASSIGNED_VALUE"] += 1
                continue
            try:
                actual_value = dec(row["settlement_value"])
                expected_dec = dec(expected_value)
            except Exception:
                checks["SETTLEMENT_VALUE_MISMATCH"] += 1
                continue
            if actual_value != expected_dec:
                checks["SETTLEMENT_VALUE_MISMATCH"] += 1
            remaining = dec(row["remaining_shares_settled"])
            expected_gross = remaining * expected_dec
            if abs(dec(row["gross_settlement_proceeds"]) - expected_gross) > Decimal("0.00001"):
                checks["SETTLEMENT_PROCEEDS_MISMATCH"] += 1
            allocation_gross = dec(conn.execute("SELECT COALESCE(SUM(CAST(gross_settlement_proceeds AS REAL)),0) v FROM settlement_allocations WHERE settlement_id=? AND mode=?", (row["settlement_id"], mode)).fetchone()["v"])
            if abs(allocation_gross - dec(row["gross_settlement_proceeds"])) > Decimal("0.00001"):
                checks["SETTLEMENT_PROCEEDS_MISMATCH"] += 1
        if mode == FORMAL:
            checks["formal_runs_missing_lock_id"] = conn.execute("SELECT COUNT(*) c FROM runs WHERE mode=? AND COALESCE(lock_id,'')=''", (mode,)).fetchone()["c"]
            checks["formal_snapshots_missing_lock_id"] = conn.execute("SELECT COUNT(*) c FROM orderbook_snapshots WHERE mode=? AND COALESCE(lock_id,'')=''", (mode,)).fetchone()["c"]
            checks["formal_entry_fills_missing_lock_id"] = conn.execute("SELECT COUNT(*) c FROM entry_fills WHERE mode=? AND COALESCE(lock_id,'')=''", (mode,)).fetchone()["c"]
            checks["formal_exit_fills_missing_lock_id"] = conn.execute("SELECT COUNT(*) c FROM exit_fills WHERE mode=? AND COALESCE(lock_id,'')=''", (mode,)).fetchone()["c"]
            checks["formal_settlements_missing_lock_id"] = conn.execute("SELECT COUNT(*) c FROM settlements WHERE mode=? AND COALESCE(lock_id,'')=''", (mode,)).fetchone()["c"]
            checks["formal_snapshot_lock_run_mismatch"] = conn.execute("SELECT COUNT(*) c FROM orderbook_snapshots ob LEFT JOIN runs r ON ob.run_id=r.run_id AND ob.mode=r.mode WHERE ob.mode=? AND COALESCE(ob.lock_id,'')!=COALESCE(r.lock_id,'')", (mode,)).fetchone()["c"]
            checks["formal_entry_lock_run_mismatch"] = conn.execute("SELECT COUNT(*) c FROM entry_fills f LEFT JOIN runs r ON f.run_id=r.run_id AND f.mode=r.mode WHERE f.mode=? AND COALESCE(f.lock_id,'')!=COALESCE(r.lock_id,'')", (mode,)).fetchone()["c"]
            checks["formal_exit_lock_run_mismatch"] = conn.execute("SELECT COUNT(*) c FROM exit_fills f LEFT JOIN runs r ON f.run_id=r.run_id AND f.mode=r.mode WHERE f.mode=? AND COALESCE(f.lock_id,'')!=COALESCE(r.lock_id,'')", (mode,)).fetchone()["c"]
            checks["formal_settlement_lock_run_mismatch"] = conn.execute("SELECT COUNT(*) c FROM settlements s LEFT JOIN runs r ON s.run_id=r.run_id AND s.mode=r.mode WHERE s.mode=? AND COALESCE(s.lock_id,'')!=COALESCE(r.lock_id,'')", (mode,)).fetchone()["c"]
        checks["missing_token_validation_for_fill"] = conn.execute("SELECT COUNT(*) c FROM entry_fills ef LEFT JOIN token_validations tv ON ef.run_id=tv.run_id AND ef.signal_id=tv.signal_id AND ef.mode=tv.mode WHERE ef.mode=? AND tv.row_id IS NULL", (mode,)).fetchone()["c"]
        checks["accepting_unknown_entered"] = 0
        checks["status_conflict_entered"] = 0
        for row in conn.execute("SELECT run_id,event_type,payload_json FROM audit_log WHERE mode=? AND event_type='market_state_observed'", (mode,)):
            try:
                payload = json.loads(row["payload_json"])
            except Exception:
                continue
            signal_id = payload.get("signal_id")
            entered = conn.execute("SELECT 1 FROM entry_fills WHERE run_id=? AND signal_id=? AND mode=? LIMIT 1", (row["run_id"], signal_id, mode)).fetchone() is not None
            exited = conn.execute("SELECT 1 FROM exit_fills WHERE run_id=? AND signal_id=? AND mode=? LIMIT 1", (row["run_id"], signal_id, mode)).fetchone() is not None
            if not (entered or exited):
                continue
            if payload.get("accepting_orders_status") == "unknown" or payload.get("market_status") == "active_accepting_orders_unknown":
                checks["accepting_unknown_entered"] += 1
            if payload.get("market_status") == "status_conflict" or payload.get("status_conflicts"):
                checks["status_conflict_entered"] += 1
        checks["fill_missing_source_hash"] = 0
        for table in ["entry_fills", "exit_fills"]:
            checks["fill_missing_source_hash"] += conn.execute(f"SELECT COUNT(*) c FROM {table} WHERE mode=? AND COALESCE(raw_response_hash,'')=''", (mode,)).fetchone()["c"]
        checks["hash_freeze_drift"] = 0
        if mode == FORMAL and formal_started:
            expected_keys = set(json.loads(get_state(conn, "expected_frozen_file_keys", "[]") or "[]"))
            current_records = frozen_file_records(root, config_path)
            if expected_keys != set(current_records):
                checks["hash_freeze_drift"] += 1
            for key in expected_keys:
                rec = current_records.get(key, {"missing": True})
                if rec.get("missing") or get_state(conn, key, "") != rec.get("sha256"):
                    checks["hash_freeze_drift"] += 1
        checks["gross_minus_fees_net_mismatch"] = 0
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
        if level == "full-replay":
            audit_http_evidence_replay(conn, mode, checks)
            audit_signal_registration_replay(conn, mode, checks)
            audit_entry_state_replay(conn, mode, checks)
            replay_books = audit_orderbook_snapshot_replay(conn, mode, checks)
            audit_fill_replay(conn, mode, checks, replay_books)
            audit_lots_allocations_and_ledger(conn, mode, checks)
            audit_positions_and_pnl(conn, mode, checks)
        elif level != "quick":
            checks["invalid_audit_level"] = 1
        ok = not any(v for v in checks.values())
        error_counts_by_code = {k: int(v) for k, v in checks.items() if isinstance(v, int) and v}
        replay_summary = {
            "signals_replayed": conn.execute("SELECT COUNT(*) c FROM signals WHERE mode=?", (mode,)).fetchone()["c"] if level == "full-replay" else 0,
            "snapshots_replayed": conn.execute("SELECT COUNT(*) c FROM orderbook_snapshots WHERE mode=?", (mode,)).fetchone()["c"] if level == "full-replay" else 0,
            "fills_replayed": conn.execute("SELECT (SELECT COUNT(*) FROM entry_fills WHERE mode=?)+(SELECT COUNT(*) FROM exit_fills WHERE mode=?) c", (mode, mode)).fetchone()["c"] if level == "full-replay" else 0,
            "fee_evidence_replayed": conn.execute("SELECT COUNT(*) c FROM http_evidence WHERE mode=? AND evidence_type IN ('gamma_market','clob_market')", (mode,)).fetchone()["c"] if level == "full-replay" else 0,
            "entry_states_rebuilt": conn.execute("SELECT COUNT(*) c FROM signals WHERE mode=?", (mode,)).fetchone()["c"] if level == "full-replay" else 0,
            "lots_rebuilt": conn.execute("SELECT COUNT(*) c FROM strategy_lots WHERE mode=?", (mode,)).fetchone()["c"] if level == "full-replay" else 0,
            "allocations_rebuilt": conn.execute("SELECT (SELECT COUNT(*) FROM exit_fill_allocations WHERE mode=?)+(SELECT COUNT(*) FROM settlement_allocations WHERE mode=?) c", (mode, mode)).fetchone()["c"] if level == "full-replay" else 0,
            "settlements_replayed": conn.execute("SELECT COUNT(*) c FROM settlements WHERE mode=?", (mode,)).fetchone()["c"] if level == "full-replay" else 0,
            "events_rebuilt": conn.execute("SELECT COUNT(*) c FROM event_results WHERE mode=?", (mode,)).fetchone()["c"] if level == "full-replay" else 0,
            "error_counts_by_code": error_counts_by_code,
        }
        return {"ok": ok, "level": level, "checks": checks, "replay_summary": replay_summary}
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
        "source": "v5.1.8_demo_fixture",
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
    # Keep the market active for one more pass so take-profit branches can
    # create exit fills from the second fixture orderbook.
    adapter = FixtureAdapter(market, clob, [books[-1]])
    exit_result = monitor_once(root, DEMO, config_path, run_id="demo_run_exit", adapter=adapter, now=parse_utc("2099-01-01T00:00:03+00:00"))
    # Flip market to resolved and settle in a second monitor pass.
    market["active"] = False
    market["closed"] = True
    market["resolved"] = True
    market["umaResolutionStatus"] = "final"
    market["winningOutcome"] = "Yes"
    market["outcomePrices"] = json.dumps(["1", "0"])
    adapter = FixtureAdapter(market, clob, [books[-1]])
    settle_result = monitor_once(root, DEMO, config_path, run_id="demo_run_settlement", adapter=adapter, now=parse_utc("2099-01-03T00:00:00+00:00"))
    return {"demo_signal_file": str(signal_file), "monitor": result, "exit": exit_result, "settlement": settle_result, "audit": audit_integrity(root, DEMO, config_path)}


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
        if len(found) >= 40:
            break
    return found


def fetch_resolved_weather_evidence(root: Path, adapter: PublicAdapter, config: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    rc7_raw = rc7_dir(root) / "resolved_market_raw"
    validations: list[dict[str, Any]] = []
    for slug in config.get("live_integration", {}).get("resolved_weather_slugs", []):
        try:
            gamma = adapter.market_by_slug(str(slug))
            condition_id = str(gamma.payload.get("conditionId") or "")
            clob = adapter.clob_market_info(condition_id) if condition_id else None
            clob_public = adapter.clob_public_market(condition_id) if condition_id else None
            pairs = clob_token_pairs(clob_public.payload if clob_public else {}) or (clob_token_pairs(clob.payload) if clob else gamma_token_pairs(gamma.payload))
            evidence = parse_settlement_evidence(gamma.payload, pairs)
            payload = {
                "slug": slug,
                "observed_at_utc": now_iso(),
                "market_endpoint": gamma.url,
                "clob_endpoint": clob.url if clob else "",
                "clob_public_market_endpoint": clob_public.url if clob_public else "",
                "gamma_status_code": gamma.status_code,
                "clob_status_code": clob.status_code if clob else None,
                "clob_public_market_status_code": clob_public.status_code if clob_public else None,
                "gamma_raw_text_sha256": sha256_text(gamma.raw_text),
                "clob_raw_text_sha256": sha256_text(clob.raw_text) if clob else "",
                "clob_public_market_raw_text_sha256": sha256_text(clob_public.raw_text) if clob_public else "",
                "gamma_payload_sha256": content_hash(gamma.payload),
                "clob_payload_sha256": content_hash(clob.payload) if clob else "",
                "clob_public_market_payload_sha256": content_hash(clob_public.payload) if clob_public else "",
                "evidence": evidence,
                "token_settlement_values": evidence.get("token_settlement_values", {}),
                "raw_gamma_text": gamma.raw_text,
                "raw_clob_text": clob.raw_text if clob else "",
                "raw_clob_public_market_text": clob_public.raw_text if clob_public else "",
            }
            validations.append(
                {
                    "slug": slug,
                    "settlement_status": evidence.get("settlement_status"),
                    "finality_status": evidence.get("finality_status"),
                    "evidence_tier": evidence.get("evidence_tier"),
                    "evidence_valid": evidence.get("evidence_valid"),
                    "token_settlement_values": evidence.get("token_settlement_values", {}),
                }
            )
            file_name = f"{str(slug).replace('/', '_')}_{content_hash(payload)[:12]}.json"
            write_json(out_dir / "resolved_market_raw" / file_name, payload)
            write_json(rc7_raw / file_name, payload)
            write_json(out_dir / "resolved_weather_evidence.json", payload)
            write_json(rc7_dir(root) / "resolved_market_validation.json", {"generated_at_utc": now_iso(), "validations": validations})
            if evidence.get("evidence_valid") and evidence.get("finality_status") == "resolved_final":
                return payload
        except Exception as exc:
            validations.append({"slug": slug, "error": str(exc)})
            write_json(out_dir / "resolved_weather_evidence_error.json", {"slug": slug, "error": str(exc)})
    result = {"error": "no_resolved_weather_evidence_found", "validations": validations}
    write_json(rc7_dir(root) / "resolved_market_validation.json", result)
    return result


def _append_live_audit(out_dir: Path, rid: str, event_type: str, **extra: Any) -> None:
    with (out_dir / "audit_log.jsonl").open("a", encoding="utf-8") as f:
        f.write(stable_json({"run_id": rid, "created_at_utc": now_iso(), "event_type": event_type, **extra}) + "\n")


def _count_live_audit_errors(out_dir: Path) -> int:
    path = out_dir / "audit_log.jsonl"
    if not path.exists():
        return 0
    return len([ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()])


def live_run_dir_from_manifest(root: Path, live_manifest: dict[str, Any], config: dict[str, Any] | None = None) -> Path | None:
    run_id = str(live_manifest.get("run_id") or "")
    if not run_id:
        return None
    cfg = config or {}
    base = data_dir(root, LIVE, cfg) if cfg else (root / "data/forward_v5_1_8/live_integration")
    path = base / run_id
    return path if path.exists() else None


def verify_live_readonly_evidence(root: Path, live_manifest: dict[str, Any] | None, live_signal: dict[str, Any] | None, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Hard-gate checks for durable same-run live evidence."""
    live_manifest = live_manifest or {}
    live_signal = live_signal or {}
    blocked: list[str] = []
    selected_markets = int(live_manifest.get("selected_market_count") or 0)
    selected_tokens = int(live_manifest.get("selected_token_count") or 0)
    snapshot_count = int(live_manifest.get("snapshot_count") or 0)
    error_count = int(live_manifest.get("error_count") or 0)
    if error_count != 0:
        blocked.append(f"error_count={error_count}")
    if selected_markets <= 0:
        blocked.append("selected_market_count_not_positive")
    if selected_tokens <= 0:
        blocked.append("selected_token_count_not_positive")
    if snapshot_count <= 0:
        blocked.append("snapshot_count_not_positive")

    out_dir = live_run_dir_from_manifest(root, live_manifest, config)
    if out_dir is None:
        blocked.append("live_run_directory_missing")
        return {
            "ok": False,
            "blocked_reasons": blocked,
            "raw_market_evidence_count": 0,
            "raw_orderbook_evidence_count": 0,
            "raw_evidence_hash_result": "fail",
            "snapshot_replay_result": "fail",
            "same_run_evidence_chain": False,
        }

    market_indexes = sorted((out_dir / "raw_markets").glob("*_index.json")) if (out_dir / "raw_markets").exists() else []
    orderbook_indexes = sorted((out_dir / "raw_orderbooks").glob("*_index.json")) if (out_dir / "raw_orderbooks").exists() else []
    raw_market_evidence_count = len(market_indexes)
    raw_orderbook_evidence_count = len(orderbook_indexes)
    if raw_market_evidence_count < selected_markets:
        blocked.append(f"raw_market_evidence_count={raw_market_evidence_count}<selected_market_count={selected_markets}")
    if raw_orderbook_evidence_count != snapshot_count:
        blocked.append(f"raw_orderbook_evidence_count={raw_orderbook_evidence_count}!=snapshot_count={snapshot_count}")

    snapshots_path = out_dir / "orderbook_snapshots.jsonl"
    snapshots: list[dict[str, Any]] = []
    if snapshots_path.exists():
        for line in snapshots_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                snapshots.append(json.loads(line))
    if len(snapshots) != snapshot_count:
        blocked.append("snapshot_jsonl_count_mismatch")

    hash_ok = True
    replay_ok = True
    snapshot_by_id = {str(s.get("snapshot_id") or ""): s for s in snapshots}

    for idx_path in market_indexes:
        try:
            index = json.loads(idx_path.read_text(encoding="utf-8"))
        except Exception as exc:
            blocked.append(f"market_index_unreadable:{idx_path.name}:{exc}")
            hash_ok = False
            continue
        for kind in ("gamma", "clob"):
            meta_rel = str(index.get(f"{kind}_evidence_path") or "")
            if not meta_rel:
                blocked.append(f"missing_market_{kind}_evidence_path:{idx_path.name}")
                hash_ok = False
                continue
            meta_path = root / meta_rel
            if not meta_path.exists():
                blocked.append(f"missing_evidence_file:{meta_rel}")
                hash_ok = False
                continue
            verified = verify_http_evidence_file(root, meta_path)
            if not verified["ok"]:
                blocked.append(f"market_{kind}_hash_fail:{meta_rel}:{','.join(verified['errors'])}")
                hash_ok = False

    for idx_path in orderbook_indexes:
        try:
            index = json.loads(idx_path.read_text(encoding="utf-8"))
        except Exception as exc:
            blocked.append(f"orderbook_index_unreadable:{idx_path.name}:{exc}")
            hash_ok = False
            continue
        for kind in ("gamma", "clob", "orderbook"):
            meta_rel = str(index.get(f"{kind}_evidence_path") or "")
            if not meta_rel:
                blocked.append(f"missing_{kind}_evidence_path:{idx_path.name}")
                hash_ok = False
                continue
            meta_path = root / meta_rel
            if not meta_path.exists():
                blocked.append(f"missing_evidence_file:{meta_rel}")
                hash_ok = False
                continue
            verified = verify_http_evidence_file(root, meta_path)
            if not verified["ok"]:
                blocked.append(f"{kind}_hash_fail:{meta_rel}:{','.join(verified['errors'])}")
                hash_ok = False
        snap_id = str(index.get("snapshot_id") or "")
        snap = snapshot_by_id.get(snap_id)
        if snap is None:
            blocked.append(f"snapshot_missing_for_index:{idx_path.name}")
            replay_ok = False
            continue
        try:
            book_meta_path = root / str(index["orderbook_evidence_path"])
            gamma_meta_path = root / str(index["gamma_evidence_path"])
            book_v = verify_http_evidence_file(root, book_meta_path)
            gamma_v = verify_http_evidence_file(root, gamma_meta_path)
            if not book_v["ok"] or not gamma_v["ok"]:
                replay_ok = False
                continue
            normalized = normalize_orderbook(
                book_v["meta"]["payload"],
                str(snap.get("token_id") or ""),
                str(snap.get("condition_id") or ""),
                gamma_v["meta"]["payload"],
            )
            if normalized["content_hash"] != snap.get("content_hash"):
                blocked.append(f"snapshot_replay_hash_mismatch:{snap_id}")
                replay_ok = False
        except Exception as exc:
            blocked.append(f"snapshot_replay_error:{snap_id}:{exc}")
            replay_ok = False

    for snap in snapshots:
        for key in ("gamma_evidence_path", "clob_evidence_path", "orderbook_evidence_path"):
            rel = str(snap.get(key) or "")
            if not rel or not (root / rel).exists():
                blocked.append(f"snapshot_missing_{key}:{snap.get('snapshot_id')}")
                hash_ok = False

    run_id = str(live_manifest.get("run_id") or "")
    same_run = True
    if live_signal.get("validation_source") != "live_readonly_saved_evidence":
        blocked.append("validation_source_not_saved_evidence")
        same_run = False
    if str(live_signal.get("run_id") or "") != run_id:
        blocked.append("signal_validation_run_id_mismatch")
        same_run = False
    snap_id = str(live_signal.get("snapshot_id") or "")
    if snap_id and snap_id not in snapshot_by_id:
        blocked.append("signal_validation_snapshot_not_in_run")
        same_run = False
    elif snap_id:
        snap = snapshot_by_id[snap_id]
        for key in ("gamma_evidence_path", "clob_evidence_path", "orderbook_evidence_path"):
            if str(live_signal.get(key) or "") != str(snap.get(key) or ""):
                blocked.append(f"signal_validation_{key}_mismatch")
                same_run = False
        for key in ("gamma_raw_bytes_sha256", "clob_raw_bytes_sha256", "orderbook_raw_bytes_sha256"):
            if live_signal.get(key) and snap.get(key) and live_signal.get(key) != snap.get(key):
                blocked.append(f"signal_validation_{key}_mismatch")
                same_run = False
        if live_signal.get("normalized_snapshot_content_hash") and live_signal.get("normalized_snapshot_content_hash") != snap.get("content_hash"):
            blocked.append("signal_validation_content_hash_mismatch")
            same_run = False
    if live_signal.get("status") != "pass":
        blocked.append(f"real_signal_to_fill_status={live_signal.get('status')}")
    if live_signal.get("uses_formal_ledger") is not False:
        blocked.append("uses_formal_ledger_not_false")
    if live_signal.get("uses_wallet_or_real_order") is not False:
        blocked.append("uses_wallet_or_real_order_not_false")

    # Deduplicate while preserving order
    deduped: list[str] = []
    seen: set[str] = set()
    for reason in blocked:
        if reason not in seen:
            deduped.append(reason)
            seen.add(reason)
    return {
        "ok": not deduped,
        "blocked_reasons": deduped,
        "raw_market_evidence_count": raw_market_evidence_count,
        "raw_orderbook_evidence_count": raw_orderbook_evidence_count,
        "raw_evidence_hash_result": "pass" if hash_ok and not any("hash" in r for r in deduped) else "fail",
        "snapshot_replay_result": "pass" if replay_ok and not any("replay" in r for r in deduped) else "fail",
        "same_run_evidence_chain": same_run and not any("run_id" in r or "snapshot_not" in r or "signal_validation" in r for r in deduped),
        "live_run_dir": repository_relative_path(root, out_dir),
    }


def build_signal_to_fill_from_saved_evidence(root: Path, rid: str, selected: list[dict[str, Any]], snapshots: list[dict[str, Any]]) -> dict[str, Any]:
    if not selected or not snapshots:
        return {
            "status": "not_run",
            "reason": "no_selected_market" if not selected else "no_orderbook_snapshots",
            "validation_source": "live_readonly_saved_evidence",
            "uses_formal_ledger": False,
            "uses_wallet_or_real_order": False,
        }
    first = selected[0]
    first_snap = next((s for s in snapshots if s.get("token_id") == first["token_id"]), None)
    if first_snap is None:
        return {
            "status": "not_run",
            "reason": "no_matching_snapshot",
            "validation_source": "live_readonly_saved_evidence",
            "run_id": rid,
            "uses_formal_ledger": False,
            "uses_wallet_or_real_order": False,
        }
    try:
        gamma_v = verify_http_evidence_file(root, root / str(first_snap["gamma_evidence_path"]))
        clob_v = verify_http_evidence_file(root, root / str(first_snap["clob_evidence_path"]))
        book_v = verify_http_evidence_file(root, root / str(first_snap["orderbook_evidence_path"]))
        if not (gamma_v["ok"] and clob_v["ok"] and book_v["ok"]):
            return {
                "status": "blocked",
                "reason": "saved_evidence_hash_failed",
                "validation_source": "live_readonly_saved_evidence",
                "run_id": rid,
                "snapshot_id": first_snap.get("snapshot_id"),
                "hash_errors": {
                    "gamma": gamma_v.get("errors"),
                    "clob": clob_v.get("errors"),
                    "orderbook": book_v.get("errors"),
                },
                "uses_formal_ledger": False,
                "uses_wallet_or_real_order": False,
            }
        gamma_payload = gamma_v["meta"]["payload"]
        clob_payload = clob_v["meta"]["payload"]
        book_payload = book_v["meta"]["payload"]
        normalized = normalize_orderbook(book_payload, first["token_id"], first["condition_id"], gamma_payload)
        if normalized["content_hash"] != first_snap.get("content_hash"):
            return {
                "status": "blocked",
                "reason": "saved_snapshot_replay_mismatch",
                "validation_source": "live_readonly_saved_evidence",
                "run_id": rid,
                "snapshot_id": first_snap.get("snapshot_id"),
                "expected_content_hash": first_snap.get("content_hash"),
                "replayed_content_hash": normalized["content_hash"],
                "uses_formal_ledger": False,
                "uses_wallet_or_real_order": False,
            }
        signal = {
            "city": first["semantic"]["city"],
            "weather_date_local": first["semantic"]["weather_date_local"],
            "weather_metric": first["semantic"]["weather_metric"],
            "temperature_bucket": first["semantic"]["canonical_label"],
            "condition_id": first["condition_id"],
            "token_id": first["token_id"],
            "outcome": first["outcome"],
        }
        validation = validate_token_mapping(signal, gamma_payload, clob_payload, normalized)
        buy = consume_buy_depth(normalized, Decimal("10"), normalized["best_ask"] if normalized["best_ask"] is not None else Decimal("1"))
        status_val = "pass" if validation["mapping_valid"] and buy["filled_shares"] > ZERO else "blocked"
        return {
            "status": status_val,
            "validation_source": "live_readonly_saved_evidence",
            "run_id": rid,
            "snapshot_id": first_snap.get("snapshot_id"),
            "market_slug": first["market_slug"],
            "condition_id": first["condition_id"],
            "token_id": first["token_id"],
            "event_key": first["semantic"]["event_key"],
            "canonical_label": first["semantic"]["canonical_label"],
            "mapping_valid": validation["mapping_valid"],
            "mapping_errors": validation["errors"],
            "entry_vwap": None if buy["vwap"] is None else dstr(buy["vwap"]),
            "filled_shares": dstr(buy["filled_shares"]),
            "filled_usd": dstr(buy["filled_usd"]),
            "gamma_evidence_path": first_snap.get("gamma_evidence_path"),
            "clob_evidence_path": first_snap.get("clob_evidence_path"),
            "orderbook_evidence_path": first_snap.get("orderbook_evidence_path"),
            "gamma_raw_bytes_sha256": first_snap.get("gamma_raw_bytes_sha256"),
            "clob_raw_bytes_sha256": first_snap.get("clob_raw_bytes_sha256"),
            "orderbook_raw_bytes_sha256": first_snap.get("orderbook_raw_bytes_sha256"),
            "normalized_snapshot_content_hash": first_snap.get("content_hash"),
            "uses_formal_ledger": False,
            "uses_wallet_or_real_order": False,
        }
    except Exception as exc:
        return {
            "status": "error",
            "validation_source": "live_readonly_saved_evidence",
            "run_id": rid,
            "error": str(exc),
            "uses_formal_ledger": False,
            "uses_wallet_or_real_order": False,
        }


def live_integration(
    root: Path,
    config_path: Path,
    iterations: int,
    interval_seconds: Decimal,
    run_id: str | None = None,
    adapter: PublicAdapter | None = None,
) -> dict[str, Any]:
    config = load_config(config_path)
    adapter = adapter or PublicAdapter(
        config["public_api"].get("gamma_base", GAMMA_BASE),
        config["public_api"].get("clob_base", CLOB_BASE),
        config["public_api"].get("timeout_seconds", 10),
        int(config["public_api"].get("max_retries", 2)),
        config["public_api"].get("backoff_seconds", Decimal("0.5")),
    )
    rid = run_id or make_run_id("live_integration")
    out_dir = data_dir(root, LIVE, config) / rid
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "raw_markets").mkdir(parents=True, exist_ok=True)
    (out_dir / "raw_orderbooks").mkdir(parents=True, exist_ok=True)
    preferred_slugs = [str(x) for x in config.get("live_integration", {}).get("preferred_weather_slugs", []) if str(x)]
    discovered = [{"slug": slug, "_event_title": ""} for slug in preferred_slugs] if preferred_slugs else discover_weather_markets(adapter, config)
    candidates: list[dict[str, Any]] = []
    seen_tokens: set[str] = set()
    for market in discovered:
        try:
            slug = str(market.get("slug") or "")
            gamma = adapter.market_by_slug(slug)
            condition_id = str(gamma.payload.get("conditionId") or "")
            clob = adapter.clob_market_info(condition_id)
            parsed = parse_weather_market(gamma.payload, str(market.get("_event_title") or ""))
            state = market_state(gamma.payload, clob.payload)
            if parsed.get("parsing_status") != "ok" or state.get("market_status") != "active_trading":
                continue
            pairs = clob_token_pairs(clob.payload) or gamma_token_pairs(gamma.payload)
            yes_pairs = [p for p in pairs if str(p.get("outcome", "")).strip().lower() == "yes"]
            chosen = (yes_pairs or pairs)[0]
            token_id = str(chosen.get("token_id") or "")
            if not token_id or token_id in seen_tokens:
                continue
            seq = len(candidates) + 1
            prefix = f"{seq:02d}_{slug}"
            gamma_meta = persist_http_result(
                root,
                out_dir / "raw_markets" / f"{prefix}_gamma.json",
                out_dir / "raw_markets" / f"{prefix}_gamma.bin",
                gamma,
            )
            clob_meta = persist_http_result(
                root,
                out_dir / "raw_markets" / f"{prefix}_clob.json",
                out_dir / "raw_markets" / f"{prefix}_clob.bin",
                clob,
            )
            index = {
                "market_slug": slug,
                "condition_id": condition_id,
                "token_id": token_id,
                "outcome": chosen.get("outcome", ""),
                "semantic": parsed,
                "market_state": state,
                "gamma_evidence_path": repository_relative_path(root, out_dir / "raw_markets" / f"{prefix}_gamma.json"),
                "clob_evidence_path": repository_relative_path(root, out_dir / "raw_markets" / f"{prefix}_clob.json"),
                "gamma_raw_bytes_sha256": gamma_meta["raw_bytes_sha256"],
                "clob_raw_bytes_sha256": clob_meta["raw_bytes_sha256"],
            }
            write_json(out_dir / "raw_markets" / f"{prefix}_index.json", index)
            candidates.append(
                {
                    "market_slug": slug,
                    "condition_id": condition_id,
                    "token_id": token_id,
                    "outcome": chosen.get("outcome", ""),
                    "market_question": gamma.payload.get("question") or gamma.payload.get("title") or "",
                    "semantic": parsed,
                    "market_state": state,
                    "gamma_evidence_path": index["gamma_evidence_path"],
                    "clob_evidence_path": index["clob_evidence_path"],
                }
            )
            seen_tokens.add(token_id)
        except Exception as exc:
            _append_live_audit(out_dir, rid, "live_market_selection_error", error=str(exc), market=market.get("slug"))
    selected: list[dict[str, Any]] = []
    selected_tokens: set[str] = set()
    selected_events: set[str] = set()
    selected_city_dates: set[str] = set()
    for bucket_preference in ("or_below", "or_higher", "exact"):
        for candidate in candidates:
            event_key = str(candidate["semantic"].get("event_key") or "")
            city_date_key = "|".join([str(candidate["semantic"].get("city") or "").lower(), str(candidate["semantic"].get("weather_date_local") or "")])
            if candidate["token_id"] in selected_tokens or city_date_key in selected_city_dates:
                continue
            if candidate["semantic"].get("bucket_type") != bucket_preference:
                continue
            selected.append(candidate)
            selected_tokens.add(candidate["token_id"])
            selected_events.add(event_key)
            selected_city_dates.add(city_date_key)
            if len(selected_city_dates) >= 2:
                break
        if len(selected_city_dates) >= 2:
            break
    for candidate in candidates:
        if len(selected) >= 3:
            break
        if candidate["token_id"] in selected_tokens:
            continue
        selected.append(candidate)
        selected_tokens.add(candidate["token_id"])
    write_json(out_dir / "selected_markets.json", selected)
    snapshots: list[dict[str, Any]] = []
    started = utcnow()
    for i in range(iterations):
        for target in selected:
            try:
                slug = str(target.get("market_slug") or "")
                gamma = adapter.market_by_slug(slug)
                condition_id = str(gamma.payload.get("conditionId") or "")
                clob = adapter.clob_market_info(condition_id)
                token = str(target["token_id"])
                book = adapter.orderbook(token)
                # Persist raw HTTP evidence BEFORE accepting the snapshot.
                seq = len(snapshots) + 1
                prefix = f"{seq:05d}_{token}"
                gamma_meta = persist_http_result(
                    root,
                    out_dir / "raw_orderbooks" / f"{prefix}_gamma.json",
                    out_dir / "raw_orderbooks" / f"{prefix}_gamma.bin",
                    gamma,
                )
                clob_meta = persist_http_result(
                    root,
                    out_dir / "raw_orderbooks" / f"{prefix}_clob.json",
                    out_dir / "raw_orderbooks" / f"{prefix}_clob.bin",
                    clob,
                )
                book_meta = persist_http_result(
                    root,
                    out_dir / "raw_orderbooks" / f"{prefix}_orderbook.json",
                    out_dir / "raw_orderbooks" / f"{prefix}_orderbook.bin",
                    book,
                )
                normalized = normalize_orderbook(book.payload, token, condition_id, gamma.payload)
                buy_probe = consume_buy_depth(normalized, Decimal("10"), normalized["best_ask"] if normalized["best_ask"] is not None else Decimal("1"))
                sell_probe = consume_sell_depth(normalized, normalized["min_order_size"])
                fee_policy = extract_fee_policy(gamma.payload, clob.payload)
                gamma_path = repository_relative_path(root, out_dir / "raw_orderbooks" / f"{prefix}_gamma.json")
                clob_path = repository_relative_path(root, out_dir / "raw_orderbooks" / f"{prefix}_clob.json")
                book_path = repository_relative_path(root, out_dir / "raw_orderbooks" / f"{prefix}_orderbook.json")
                snapshot_id = id_for("live", {"run_id": rid, "token": token, "content": normalized["content_hash"]})
                row = {
                    "run_id": rid,
                    "iteration": i + 1,
                    "captured_at_utc": book.received_at_utc,
                    "market_slug": slug,
                    "condition_id": condition_id,
                    "token_id": token,
                    "outcome": target.get("outcome", ""),
                    "event_key": target.get("semantic", {}).get("event_key", ""),
                    "city": target.get("semantic", {}).get("city", ""),
                    "weather_date_local": target.get("semantic", {}).get("weather_date_local", ""),
                    "weather_metric": target.get("semantic", {}).get("weather_metric", ""),
                    "bucket_type": target.get("semantic", {}).get("bucket_type", ""),
                    "canonical_label": target.get("semantic", {}).get("canonical_label", ""),
                    "parsing_status": target.get("semantic", {}).get("parsing_status", ""),
                    "snapshot_id": snapshot_id,
                    "content_hash": normalized["content_hash"],
                    "best_bid": normalized["best_bid"],
                    "best_ask": normalized["best_ask"],
                    "spread": normalized["spread"],
                    "tick_size": normalized["tick_size"],
                    "min_order_size": normalized["min_order_size"],
                    "bid_depth_levels": normalized["bid_depth_levels"],
                    "ask_depth_levels": normalized["ask_depth_levels"],
                    "total_bid_shares": normalized["total_bid_shares"],
                    "total_ask_shares": normalized["total_ask_shares"],
                    "buy_probe_status": buy_probe["status"],
                    "buy_probe_vwap": buy_probe["vwap"],
                    "sell_probe_status": sell_probe["status"],
                    "sell_probe_vwap": sell_probe["vwap"],
                    "fee_crosscheck_status": fee_policy["fee_crosscheck_status"],
                    "no_bid": not bool(normalized["bids"]),
                    "no_ask": not bool(normalized["asks"]),
                    "endpoint": book.url,
                    "gamma_evidence_path": gamma_path,
                    "clob_evidence_path": clob_path,
                    "orderbook_evidence_path": book_path,
                    "gamma_raw_bytes_sha256": gamma_meta["raw_bytes_sha256"],
                    "clob_raw_bytes_sha256": clob_meta["raw_bytes_sha256"],
                    "orderbook_raw_bytes_sha256": book_meta["raw_bytes_sha256"],
                }
                capture_index = {
                    "run_id": rid,
                    "snapshot_id": snapshot_id,
                    "market_slug": slug,
                    "condition_id": condition_id,
                    "token_id": token,
                    "content_hash": normalized["content_hash"],
                    "gamma_evidence_path": gamma_path,
                    "clob_evidence_path": clob_path,
                    "orderbook_evidence_path": book_path,
                    "gamma_raw_bytes_sha256": gamma_meta["raw_bytes_sha256"],
                    "clob_raw_bytes_sha256": clob_meta["raw_bytes_sha256"],
                    "orderbook_raw_bytes_sha256": book_meta["raw_bytes_sha256"],
                    "payload_sha256": {
                        "gamma": gamma_meta["payload_sha256"],
                        "clob": clob_meta["payload_sha256"],
                        "orderbook": book_meta["payload_sha256"],
                    },
                }
                write_json(out_dir / "raw_orderbooks" / f"{prefix}_index.json", capture_index)
                snapshots.append(row)
            except Exception as exc:
                _append_live_audit(
                    out_dir,
                    rid,
                    "live_orderbook_error",
                    error=str(exc),
                    market=target.get("market_slug"),
                    token_id=target.get("token_id"),
                )
        if i < iterations - 1 and interval_seconds > ZERO:
            time.sleep(float(interval_seconds))
    with (out_dir / "orderbook_snapshots.jsonl").open("w", encoding="utf-8") as f:
        for row in snapshots:
            f.write(stable_json(row) + "\n")
    evidence = fetch_resolved_weather_evidence(root, adapter, config, out_dir)
    signal_to_fill = build_signal_to_fill_from_saved_evidence(root, rid, selected, snapshots)
    ended = utcnow()
    event_keys = sorted({str(s.get("event_key", "")) for s in snapshots if s.get("event_key")})
    city_date_keys = sorted({"|".join([str(s.get("city", "")).lower(), str(s.get("weather_date_local", ""))]) for s in snapshots if s.get("city") and s.get("weather_date_local")})
    formal_counts = status(root, FORMAL, config_path)
    evidence_check = verify_live_readonly_evidence(
        root,
        {
            "run_id": rid,
            "selected_market_count": len({str(s.get("market_slug", "")) for s in snapshots}),
            "selected_token_count": len({str(s.get("token_id", "")) for s in snapshots}),
            "snapshot_count": len(snapshots),
            "error_count": _count_live_audit_errors(out_dir),
        },
        signal_to_fill,
        config,
    )
    manifest = {
        "run_id": rid,
        "started_at_utc": started.isoformat(),
        "ended_at_utc": ended.isoformat(),
        "duration_seconds": dstr(Decimal(str((ended - started).total_seconds()))),
        "selected_market_count": len({str(s.get("market_slug", "")) for s in snapshots}),
        "selected_event_count": len(event_keys),
        "selected_event_keys": event_keys,
        "selected_city_date_count": len(city_date_keys),
        "selected_city_date_keys": city_date_keys,
        "selected_tokens": sorted({str(s.get("token_id", "")) for s in snapshots}),
        "selected_token_count": len({str(s.get("token_id", "")) for s in snapshots}),
        "snapshots_per_token": {token: len([s for s in snapshots if s.get("token_id") == token]) for token in sorted({str(s.get("token_id", "")) for s in snapshots})},
        "bucket_types": sorted({str(s.get("bucket_type", "")) for s in snapshots if s.get("bucket_type")}),
        "snapshot_count": len(snapshots),
        "error_count": _count_live_audit_errors(out_dir),
        "raw_market_evidence_count": evidence_check.get("raw_market_evidence_count"),
        "raw_orderbook_evidence_count": evidence_check.get("raw_orderbook_evidence_count"),
        "raw_evidence_hash_result": evidence_check.get("raw_evidence_hash_result"),
        "snapshot_replay_result": evidence_check.get("snapshot_replay_result"),
        "same_run_evidence_chain": evidence_check.get("same_run_evidence_chain"),
        "evidence_blocked_reasons": evidence_check.get("blocked_reasons"),
        "adapter_version": ADAPTER_VERSION,
        "actual_endpoints": adapter.visited_endpoints,
        "resolved_evidence_status": evidence.get("evidence", {}).get("settlement_status", evidence.get("error", "")),
        "formal_signal_fill_position_counts": formal_counts,
        "code_hash": current_hashes(root, config_path),
        "real_signal_to_fill_validation": signal_to_fill,
        "validation_source": "live_readonly_saved_evidence",
    }
    write_json(out_dir / "run_manifest.json", manifest)
    write_json(rc7_dir(root) / "live_run_manifest.json", manifest)
    write_json(rc7_dir(root) / "real_signal_to_fill_validation.json", signal_to_fill)
    write_json(out_dir / "evidence_verification.json", evidence_check)
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
    write_json(root / "data/forward_v5_1_8/formal_empty_proof.json", proof)
    write_json(rc7_dir(root) / "formal_empty_proof.json", proof)
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
    sp = sub.add_parser("run-loop"); sp.add_argument("--mode", choices=[FORMAL, DEMO], default=FORMAL); sp.add_argument("--iterations", type=int, default=1); sp.add_argument("--interval-seconds", default="0"); sp.add_argument("--run-id"); sp.add_argument("--confirm-infinite", action="store_true"); sp.add_argument("--recover-stale-lock", action="store_true")
    sp = sub.add_parser("pause"); sp.add_argument("--mode", choices=[FORMAL, DEMO], default=FORMAL)
    sp = sub.add_parser("resume"); sp.add_argument("--mode", choices=[FORMAL, DEMO], default=FORMAL)
    sp = sub.add_parser("stop"); sp.add_argument("--mode", choices=[FORMAL, DEMO], default=FORMAL)
    sp = sub.add_parser("demo-run")
    sp = sub.add_parser("live-integration"); sp.add_argument("--iterations", type=int, default=1); sp.add_argument("--interval-seconds", default="0"); sp.add_argument("--run-id")
    sp = sub.add_parser("status"); sp.add_argument("--mode", choices=[FORMAL, DEMO], default=FORMAL)
    sp = sub.add_parser("audit-integrity"); sp.add_argument("--mode", choices=[FORMAL, DEMO], default=FORMAL); sp.add_argument("--level", choices=["quick", "full-replay"], default="full-replay")
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
        print(json.dumps(run_loop(root, args.mode, config_path, args.iterations, dec(args.interval_seconds), args.run_id, args.confirm_infinite, args.recover_stale_lock), indent=2, ensure_ascii=False, sort_keys=True))
    elif args.command in {"pause", "resume", "stop"}:
        print(json.dumps(monitor_control(root, args.mode, config_path, args.command), indent=2, ensure_ascii=False, sort_keys=True))
    elif args.command == "demo-run":
        print(json.dumps(json_safe(demo_run(root, config_path)), indent=2, ensure_ascii=False, sort_keys=True))
    elif args.command == "live-integration":
        print(json.dumps(json_safe(live_integration(root, config_path, args.iterations, dec(args.interval_seconds), args.run_id)), indent=2, ensure_ascii=False, sort_keys=True))
    elif args.command == "status":
        print(json.dumps(status(root, args.mode, config_path), indent=2, ensure_ascii=False, sort_keys=True))
    elif args.command == "audit-integrity":
        print(json.dumps(audit_integrity(root, args.mode, config_path, args.level), indent=2, ensure_ascii=False, sort_keys=True))
    elif args.command == "formal-empty-proof":
        print(json.dumps(formal_empty_proof(root, config_path), indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
