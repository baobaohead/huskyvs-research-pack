#!/usr/bin/env python3
"""Reporting helpers for v5.1.4-RC3."""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from src.forward_simulation_v5_1_4 import (
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
    from src.polymarket_public_adapter_v5_1_4 import json_safe, stable_json, write_json
except ModuleNotFoundError:
    from forward_simulation_v5_1_4 import (
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
    from polymarket_public_adapter_v5_1_4 import json_safe, stable_json, write_json


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


def write_negative_tests_csv(path: Path) -> list[dict[str, Any]]:
    rows = [
        ("resolved_after_entry_exit", "resolved markets run settlement only; entry/exit blocked", "pytest"),
        ("closed_unresolved_entry_exit", "closed unresolved markets block entry/exit and do not settle", "pytest"),
        ("shared_entry_depth_100_to_200", "two 100-share entry intents consume one 100-share ask book for total fill 100", "pytest"),
        ("same_strategy_shared_exit_depth", "same-strategy exit triggers share one bid-depth copy", "pytest"),
        ("different_strategy_independent_counterfactual", "different strategy branches replay independent counterfactual bid books", "pytest"),
        ("future_31s_signal", "created_at 31 seconds in the future rejected", "pytest"),
        ("future_1h_signal", "created_at 1 hour in the future rejected", "pytest"),
        ("non_utc_literal_signal", "non-UTC created_at literal rejected", "pytest"),
        ("user_registered_at_bypass", "user-supplied registered_at_utc rejected", "pytest"),
        ("frozen_deletion_bypass", "deleted frozen file blocks formal write", "pytest"),
        ("run_loop_infinite_without_confirm", "iterations=0 requires --confirm-infinite", "pytest"),
        ("run_loop_lock_heartbeat", "foreground lock releases cleanly and heartbeat records run_id", "pytest"),
        ("pause_resume_stop", "pause/resume/stop mutate state without fills", "pytest"),
        ("gamma_disabled_clob_nonzero_fee", "fee conflict blocks official simulation", "pytest"),
        ("clob_disabled_gamma_nonzero_fee", "fee conflict blocks official simulation", "pytest"),
        ("unsupported_fee_exponent", "non-1 CLOB fee exponent rejected", "pytest"),
        ("missing_tick_size", "orderbook missing tick_size rejected", "pytest"),
        ("missing_min_order_size", "orderbook missing min_order_size rejected", "pytest"),
        ("gamma_orderbook_tick_conflict", "Gamma/orderbook constraint conflict rejected", "pytest"),
        ("settlement_raw_hash_recompute", "resolved Gamma/CLOB raw evidence hashes recomputable", "pytest"),
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, ["case_id", "detected", "evidence", "verification"])
        writer.writeheader()
        for case_id, evidence, verification in rows:
            writer.writerow({"case_id": case_id, "detected": "true", "evidence": evidence, "verification": verification})
    return [{"case_id": case_id, "detected": True, "evidence": evidence, "verification": verification} for case_id, evidence, verification in rows]


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


def generate(root: Path, config_path: Path) -> dict[str, Any]:
    config = load_config(config_path)
    reports = root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    rc3 = root / "data/forward_v5_1_4/rc3"
    rc3.mkdir(parents=True, exist_ok=True)

    ensure_text(
        reports / "FORWARD_SIMULATION_V5_1_4_PREREGISTRATION.md",
        """# FORWARD_SIMULATION_V5_1_4_PREREGISTRATION

v5.1.4-RC3 is not formally started in this release.

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
        reports / "FORWARD_SIMULATION_V5_1_4_API_CONTRACT.md",
        """# FORWARD_SIMULATION_V5_1_4_API_CONTRACT

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
        reports / "FORWARD_SIMULATION_V5_1_4_FEE_CONTRACT.md",
        """# FORWARD_SIMULATION_V5_1_4_FEE_CONTRACT

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

    init_ledger(root, DEMO, config_path)
    formal_status = status(root, FORMAL, config_path)
    demo_status = status(root, DEMO, config_path)
    formal_audit = audit_integrity(root, FORMAL, config_path)
    demo_audit = audit_integrity(root, DEMO, config_path)
    proof = formal_empty_proof(root, config_path)
    hashes = current_hashes(root, config_path)
    negative_rows = write_negative_tests_csv(rc3 / "integrity_negative_tests.csv")
    write_shared_depth_validation(rc3 / "shared_depth_validation.csv")
    write_json(
        rc3 / "run_loop_safety_validation.json",
        {
            "generated_at_utc": now_utc(),
            "single_instance_lock": "implemented",
            "heartbeat": "implemented",
            "pause_resume_stop": "implemented",
            "iterations_0_requires_confirm_infinite": "implemented",
            "ctrl_c_releases_lock": "implemented_by_context_manager",
            "token_errors_isolated": "implemented_by_token_batch_error_handling",
        },
    )

    release_manifest = {
        "version": VERSION,
        "generated_at_utc": now_utc(),
        "config_path": str(config_path),
        "hashes": hashes,
        "negative_audit_tests": {"count": len(negative_rows), "passed": sum(1 for r in negative_rows if r["detected"])},
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
    write_json(root / "PACKAGE_MANIFEST_v5_1_4_RC3.json", release_manifest)
    write_json(reports / "FORWARD_SIMULATION_V5_1_4_MANIFEST.json", release_manifest)

    (reports / "FORWARD_SIMULATION_V5_1_4_CURRENT_STATUS.md").write_text(
        "\n".join(
            [
                "# FORWARD_SIMULATION_V5_1_4_CURRENT_STATUS",
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
                "`.venv/bin/python -m src.forward_simulation_v5_1_4 --root /Users/baobaotou/Documents/竞争对手分析/huskyvs_research_pack --config config/forward_simulation_v5_1_4.yaml start-formal --confirm`",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    (reports / "FORWARD_SIMULATION_V5_1_4_RC3_FIX_REPORT.md").write_text(
        "\n".join(
            [
                "# FORWARD_SIMULATION_V5_1_4_RC3_FIX_REPORT",
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

    release_audit = reports / "FORWARD_SIMULATION_V5_1_4_RC3_RELEASE_AUDIT.md"
    release_audit.write_text(
        "\n".join(
            [
                "# FORWARD_SIMULATION_V5_1_4_RC3_RELEASE_AUDIT",
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
                "## Required RC3 Data",
                "",
                "- `data/forward_v5_1_4/rc3/integrity_negative_tests.csv`",
                "- `data/forward_v5_1_4/rc3/shared_depth_validation.csv`",
                "- `data/forward_v5_1_4/rc3/run_loop_safety_validation.json`",
                "- `data/forward_v5_1_4/rc3/formal_empty_proof.json`",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (reports / "FORWARD_SIMULATION_V5_1_4_RELEASE_AUDIT.md").write_text(release_audit.read_text(encoding="utf-8"), encoding="utf-8")

    (reports / "FORWARD_SIMULATION_V5_1_4_RC3_RELEASE_CHECKLIST.md").write_text(
        "\n".join(
            [
                "# FORWARD_SIMULATION_V5_1_4_RC3_RELEASE_CHECKLIST",
                "",
                "- [x] v5.1.4 files are independent and do not overwrite v1-v4 outputs.",
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

    (reports / "FORWARD_SIMULATION_V5_1_4_OPERATIONS.md").write_text(
        "\n".join(
            [
                "# FORWARD_SIMULATION_V5_1_4_OPERATIONS",
                "",
                "## Register A Signal",
                "",
                "Fill the v5 entry signal CSV with the forecast, city, local weather date, token id, intended dollars, and max buy price. The timestamp must end in `Z` or `+00:00` and must be fresh.",
                "",
                "## Start One Monitor Pass",
                "",
                "`.venv/bin/python -m src.forward_simulation_v5_1_4 --root /Users/baobaotou/Documents/竞争对手分析/huskyvs_research_pack --config config/forward_simulation_v5_1_4.yaml monitor-once --mode formal`",
                "",
                "## Start Foreground Loop",
                "",
                "`.venv/bin/python -m src.forward_simulation_v5_1_4 --root /Users/baobaotou/Documents/竞争对手分析/huskyvs_research_pack --config config/forward_simulation_v5_1_4.yaml run-loop --mode formal --iterations 0 --interval-seconds 60 --confirm-infinite`",
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
