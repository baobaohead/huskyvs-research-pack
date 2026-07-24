#!/usr/bin/env python3
"""Reporting helpers for SQLite-backed forward simulation v5.1.1 RC1."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from src.forward_simulation_v5_1_1 import FORMAL, DEMO, STRATEGY_IDS, aggregate_results, audit_integrity, now_iso, status


def fnum(value: Any, default: float = 0.0) -> float:
    try:
        if value in ("", None):
            return default
        x = float(value)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def money(value: Any) -> str:
    x = fnum(value, math.nan)
    return "n/a" if not math.isfinite(x) else f"${x:,.2f}"


def table(rows: list[dict[str, Any]], cols: list[tuple[str, str]]) -> str:
    if not rows:
        return "_No rows._"
    out = ["| " + " | ".join(h for h, _ in cols) + " |", "| " + " | ".join("---" for _ in cols) + " |"]
    for row in rows:
        out.append("| " + " | ".join(str(row.get(k, "")) for _, k in cols) + " |")
    return "\n".join(out)


def summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for strategy_id in STRATEGY_IDS:
        rs = [r for r in rows if r["strategy_id"] == strategy_id]
        out.append({
            "strategy_id": strategy_id,
            "traded_events": sum(fnum(r["traded_event_count"]) for r in rs),
            "settled_events": sum(fnum(r["settled_event_count"]) for r in rs),
            "gross_entry_cost": money(sum(fnum(r["gross_entry_cost"]) for r in rs)),
            "total_fees": money(sum(fnum(r["total_fees"]) for r in rs)),
            "net_pnl": money(sum(fnum(r.get("net_pnl")) for r in rs if r.get("net_pnl") is not None)),
            "triggered_events": sum(fnum(r["triggered_take_profit"]) for r in rs),
        })
    return out


def generate(root: Path, config_path: Path, output: Path) -> dict[str, Any]:
    formal_rows = aggregate_results(root, FORMAL, config_path)
    demo_rows = aggregate_results(root, DEMO, config_path)
    formal_status = status(root, FORMAL, config_path)
    demo_status = status(root, DEMO, config_path)
    formal_integrity = audit_integrity(root, FORMAL, config_path)
    demo_integrity = audit_integrity(root, DEMO, config_path)
    lines = [
        "# FORWARD_SIMULATION_V5_1_1_STATUS",
        "",
        f"Generated at: {now_iso()}",
        "",
        "## Formal",
        "",
        f"- Formal started: {bool(formal_status.get('formal_started_at_utc'))}",
        f"- Ledger: `{formal_status['ledger_path']}`",
        f"- Config: `{formal_status['config_path']}`",
        f"- Registered signals: {formal_status['registered_signal_count']}",
        f"- Traded events: {formal_status['traded_event_count']}",
        f"- Settled events: {formal_status['settled_event_count']}",
        f"- Remaining to 50: {formal_status['remaining_to_50_settled_events']}",
        f"- Integrity ok: {formal_integrity['ok']}",
        "",
        table(summarize(formal_rows), [("Strategy", "strategy_id"), ("Traded Events", "traded_events"), ("Settled Events", "settled_events"), ("Entry", "gross_entry_cost"), ("Fees", "total_fees"), ("Net PnL", "net_pnl"), ("TP Events", "triggered_events")]),
        "",
        "## Demo",
        "",
        f"- Registered signals: {demo_status['registered_signal_count']}",
        f"- Traded events: {demo_status['traded_event_count']}",
        f"- Settled events: {demo_status['settled_event_count']}",
        f"- Integrity ok: {demo_integrity['ok']}",
        "",
        table(summarize(demo_rows), [("Strategy", "strategy_id"), ("Traded Events", "traded_events"), ("Settled Events", "settled_events"), ("Entry", "gross_entry_cost"), ("Fees", "total_fees"), ("Net PnL", "net_pnl"), ("TP Events", "triggered_events")]),
        "",
        "No wallet, signing, private-key, or real order-submission code is present.",
    ]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"report_path": str(output), "formal_integrity_ok": formal_integrity["ok"], "demo_integrity_ok": demo_integrity["ok"]}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", default="reports/FORWARD_SIMULATION_V5_1_1_STATUS.md")
    args = parser.parse_args()
    print(json.dumps(generate(Path(args.root), Path(args.config), Path(args.output)), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
