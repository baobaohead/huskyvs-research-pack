#!/usr/bin/env python3
"""Reporting helpers for v5.1.3-RC2."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from src.forward_simulation_v5_1_3 import (
        DEMO,
        FORMAL,
        VERSION,
        audit_integrity,
        current_hashes,
        data_dir,
        db_path,
        formal_empty_proof,
        init_ledger,
        load_config,
        status,
    )
    from src.polymarket_public_adapter_v5_1_3 import json_safe, stable_json, write_json
except ModuleNotFoundError:
    from forward_simulation_v5_1_3 import (
        DEMO,
        FORMAL,
        VERSION,
        audit_integrity,
        current_hashes,
        data_dir,
        db_path,
        formal_empty_proof,
        init_ledger,
        load_config,
        status,
    )
    from polymarket_public_adapter_v5_1_3 import json_safe, stable_json, write_json


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


def generate(root: Path, config_path: Path) -> dict[str, Any]:
    config = load_config(config_path)
    init_ledger(root, DEMO, config_path)
    formal_status = status(root, FORMAL, config_path)
    demo_status = status(root, DEMO, config_path)
    formal_audit = audit_integrity(root, FORMAL, config_path)
    demo_audit = audit_integrity(root, DEMO, config_path)
    proof = formal_empty_proof(root, config_path)
    hashes = current_hashes(root, config_path)
    reports = root / "reports"
    reports.mkdir(parents=True, exist_ok=True)

    release_manifest = {
        "version": VERSION,
        "generated_at_utc": now_utc(),
        "config_path": str(config_path),
        "hashes": hashes,
        "formal_empty_proof": proof,
        "formal_audit": formal_audit,
        "demo_audit": demo_audit,
        "formal_runs": read_runs(root, FORMAL, config),
        "demo_runs": read_runs(root, DEMO, config),
        "official_docs": {
            "fees": "https://docs.polymarket.com/trading/fees",
            "orderbook": "https://docs.polymarket.com/api-reference/market-data/get-order-book",
            "market_by_slug": "https://docs.polymarket.com/api-reference/markets/get-market-by-slug",
            "public_methods": "https://docs.polymarket.com/trading/clients/public",
        },
    }
    write_json(root / "PACKAGE_MANIFEST_v5_1_3_RC2.json", release_manifest)

    (reports / "FORWARD_SIMULATION_V5_1_3_CURRENT_STATUS.md").write_text(
        "\n".join(
            [
                "# FORWARD_SIMULATION_V5_1_3_CURRENT_STATUS",
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
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    (reports / "FORWARD_SIMULATION_V5_1_3_RELEASE_AUDIT.md").write_text(
        "\n".join(
            [
                "# FORWARD_SIMULATION_V5_1_3_RELEASE_AUDIT",
                "",
                f"Generated at: {now_utc()}",
                "",
                "## Conclusion",
                "",
                "v5.1.3-RC2 merges the v5.1.1-RC1 ledger pattern with the v5.1.2 public adapter into a standalone module set.",
                "",
                "## Blocking Fixes",
                "",
                "- Public adapter is called by `monitor-once` and `run-loop` for market detail, CLOB fee parameters, order books, and settlement status.",
                "- Buy fees increase cost; sell fees reduce proceeds; settlement fee status is explicit.",
                "- Fee policy is CLOB-primary and Gamma-crosschecked; unknown/conflict does not become zero.",
                "- Settlement requires resolved official evidence and never defaults a missing winner to zero.",
                "- Token mapping crosschecks Gamma outcomes/token IDs, CLOB token mapping, orderbook asset/condition, and signal metadata.",
                "- Every run writes a `run_id`; reports and manifests are run-aware.",
                "- Tick size and min order size are parsed with Decimal and enforced before simulated fills.",
                "",
                "## Integrity",
                "",
                f"- Formal empty proof: {proof['ok']}",
                f"- Formal audit: {formal_audit['ok']}",
                f"- Demo audit: {demo_audit['ok']}",
                "",
                "## Official API Notes",
                "",
                "The implementation follows Polymarket public GET endpoints and fee/orderbook fields documented in the official Fees, Public Methods, Get Order Book, and Get Market by Slug pages.",
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
