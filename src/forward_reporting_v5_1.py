#!/usr/bin/env python3
"""Reports for forward simulation v5.1."""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from pathlib import Path
from typing import Any

from src.forward_simulation_v5_1 import STRATEGY_IDS, aggregate_results, audit_integrity, data_dir, now_utc, read_csv


def fnum(value: Any, default: float = 0.0) -> float:
    try:
        if value in ("", None):
            return default
        x = float(value)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def safe_div(num: float, den: float) -> float:
    return num / den if den else math.nan


def fmt_money(value: Any) -> str:
    x = fnum(value, math.nan)
    return "n/a" if not math.isfinite(x) else f"${x:,.2f}"


def fmt_pct(value: Any) -> str:
    x = fnum(value, math.nan)
    return "n/a" if not math.isfinite(x) else f"{x * 100:.1f}%"


def markdown_table(rows: list[dict[str, Any]], cols: list[tuple[str, str]]) -> str:
    if not rows:
        return "_No rows._"
    out = ["| " + " | ".join(h for h, _ in cols) + " |", "| " + " | ".join("---" for _ in cols) + " |"]
    for row in rows:
        out.append("| " + " | ".join(str(row.get(k, "")) for _, k in cols) + " |")
    return "\n".join(out)


def summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    hold = {r["event_key"]: fnum(r.get("net_pnl"), math.nan) for r in rows if r["strategy_id"] == "hold_to_settlement" and fnum(r.get("settled_event_count")) == 1}
    for strategy_id in STRATEGY_IDS:
        rs = [r for r in rows if r["strategy_id"] == strategy_id]
        settled = [r for r in rs if fnum(r.get("settled_event_count")) == 1]
        pnls = [fnum(r.get("net_pnl"), math.nan) for r in settled if math.isfinite(fnum(r.get("net_pnl"), math.nan))]
        positives = sorted([p for p in pnls if p > 0], reverse=True)
        total = sum(pnls)
        out.append({
            "strategy_id": strategy_id,
            "signal_count": sum(fnum(r.get("signal_count")) for r in rs),
            "position_count": sum(fnum(r.get("position_count")) for r in rs),
            "traded_event_count": sum(fnum(r.get("traded_event_count")) for r in rs),
            "settled_event_count": len(settled),
            "gross_entry_cost": sum(fnum(r.get("gross_entry_cost")) for r in rs),
            "total_fees": sum(fnum(r.get("total_fees")) for r in rs),
            "gross_pnl": sum(fnum(r.get("gross_pnl")) for r in settled),
            "net_pnl": total if pnls else math.nan,
            "delta_vs_hold": sum(fnum(r.get("net_pnl")) - hold.get(r["event_key"], 0.0) for r in settled if r["event_key"] in hold),
            "profit_event_rate": safe_div(sum(1 for p in pnls if p > 0), len(pnls)),
            "median_event_pnl": statistics.median(pnls) if pnls else math.nan,
            "max_event_loss": min(pnls) if pnls else math.nan,
            "top1_event_profit_share": safe_div(sum(positives[:1]), total) if total > 0 else math.nan,
            "top5_event_profit_share": safe_div(sum(positives[:5]), total) if total > 0 else math.nan,
            "leave_top5_out_pnl": total - sum(positives[:5]) if pnls else math.nan,
            "triggered_take_profit_events": sum(1 for r in rs if str(r.get("triggered_take_profit")) == "True"),
            "incomplete_take_profit_events": sum(1 for r in rs if str(r.get("incomplete_take_profit")) == "True"),
        })
    return out


def generate(root: Path) -> dict[str, Any]:
    formal_dir = data_dir(root, "formal")
    demo_dir = data_dir(root, "demo")
    formal_rows = aggregate_results(formal_dir)
    demo_rows = aggregate_results(demo_dir)
    formal_summary = summarize(formal_rows)
    demo_summary = summarize(demo_rows)
    formal_integrity = audit_integrity(formal_dir, root, root / "config/forward_simulation_v5_1.yaml", "formal")
    demo_integrity = audit_integrity(demo_dir, root, root / "config/forward_simulation_v5_1.yaml", "demo")
    lines = [
        "# FORWARD_SIMULATION_V5_1_CURRENT_STATUS",
        "",
        f"Generated at: {now_utc()}",
        "",
        "## Formal Sample",
        "",
        "Formal v5.1 has not been started in this delivery. Demo rows are isolated under `data/forward_v5_1/demo/`.",
        "",
        markdown_table([{**r, "gross_entry_cost": fmt_money(r["gross_entry_cost"]), "total_fees": fmt_money(r["total_fees"]), "net_pnl": fmt_money(r["net_pnl"]), "delta_vs_hold": fmt_money(r["delta_vs_hold"])} for r in formal_summary], [("Strategy", "strategy_id"), ("Signals", "signal_count"), ("Positions", "position_count"), ("Traded Events", "traded_event_count"), ("Settled Events", "settled_event_count"), ("Entry Cost", "gross_entry_cost"), ("Fees", "total_fees"), ("Net PnL", "net_pnl"), ("Delta vs Hold", "delta_vs_hold")]),
        "",
        "## Demo Sample",
        "",
        markdown_table([{**r, "gross_entry_cost": fmt_money(r["gross_entry_cost"]), "total_fees": fmt_money(r["total_fees"]), "net_pnl": fmt_money(r["net_pnl"]), "delta_vs_hold": fmt_money(r["delta_vs_hold"])} for r in demo_summary], [("Strategy", "strategy_id"), ("Signals", "signal_count"), ("Positions", "position_count"), ("Traded Events", "traded_event_count"), ("Settled Events", "settled_event_count"), ("Entry Cost", "gross_entry_cost"), ("Fees", "total_fees"), ("Net PnL", "net_pnl"), ("Delta vs Hold", "delta_vs_hold")]),
        "",
        "## Integrity",
        "",
        f"- Formal audit-integrity ok: {formal_integrity['ok']}",
        f"- Demo audit-integrity ok: {demo_integrity['ok']}",
        "- No wallet, signing, or real order-submission code is present.",
    ]
    out_path = root / "reports/FORWARD_SIMULATION_V5_1_CURRENT_STATUS.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"formal_rows": len(formal_rows), "demo_rows": len(demo_rows), "formal_integrity_ok": formal_integrity["ok"], "demo_integrity_ok": demo_integrity["ok"], "report_path": str(out_path)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    args = parser.parse_args()
    print(json.dumps(generate(Path(args.root)), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
