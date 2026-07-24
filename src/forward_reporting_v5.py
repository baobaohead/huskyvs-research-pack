#!/usr/bin/env python3
"""Reporting and integrity helpers for forward simulation v5."""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STRATEGY_IDS = [
    "hold_to_settlement",
    "tp_2x_sell_50pct",
    "tp_2x_sell_75pct",
    "tp_5x_sell_25pct",
]

EVENT_RESULT_FIELDS = [
    "event_id",
    "strategy_id",
    "mode",
    "city",
    "weather_date_local",
    "market_slug",
    "position_count",
    "simulated_buy_usd",
    "simulated_entry_spent_usd",
    "unfilled_entry_usd",
    "avg_entry_vwap",
    "avg_exit_vwap",
    "gross_pnl",
    "net_pnl",
    "triggered_take_profit",
    "incomplete_take_profit",
    "settled",
]


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def data_dir_for(root: Path, mode: str) -> Path:
    return root / "data/forward_v5/demo" if mode == "demo" else root / "data/forward_v5"


def aggregate_event_results(data_dir: Path, mode: str) -> list[dict[str, Any]]:
    events = {row["signal_id"]: row for row in read_csv(data_dir / "events.csv")}
    entries = read_csv(data_dir / "entry_fills.csv")
    exits = read_csv(data_dir / "exit_fills.csv")
    settlements = read_csv(data_dir / "settlements.csv")
    rows: list[dict[str, Any]] = []

    entry_by_signal: dict[str, dict[str, Any]] = defaultdict(lambda: defaultdict(float))
    for row in entries:
        agg = entry_by_signal[row["signal_id"]]
        agg["intended"] += fnum(row.get("intended_usd"))
        agg["spent"] += fnum(row.get("spent_usd"))
        agg["unfilled"] += fnum(row.get("unfilled_usd"))
        agg["shares"] += fnum(row.get("filled_shares"))
        agg["entry_value"] += fnum(row.get("entry_vwap")) * fnum(row.get("filled_shares"))

    exit_by_signal_strategy: dict[tuple[str, str], dict[str, Any]] = defaultdict(lambda: defaultdict(float))
    for row in exits:
        agg = exit_by_signal_strategy[(row["signal_id"], row["strategy_id"])]
        agg["proceeds"] += fnum(row.get("gross_proceeds"))
        agg["shares"] += fnum(row.get("filled_shares"))
        agg["exit_value"] += fnum(row.get("exit_vwap")) * fnum(row.get("filled_shares"))
        agg["triggered"] = 1
        if str(row.get("complete_fill")) != "True":
            agg["incomplete"] = 1

    settlement_by_signal_strategy = {(row["signal_id"], row["strategy_id"]): row for row in settlements}
    for signal_id, event in events.items():
        entry = entry_by_signal.get(signal_id, {})
        for strategy_id in STRATEGY_IDS:
            exit_agg = exit_by_signal_strategy.get((signal_id, strategy_id), {})
            settlement = settlement_by_signal_strategy.get((signal_id, strategy_id), {})
            rows.append(
                {
                    "event_id": event.get("event_id", ""),
                    "strategy_id": strategy_id,
                    "mode": mode,
                    "city": event.get("city", ""),
                    "weather_date_local": event.get("weather_date_local", ""),
                    "market_slug": event.get("market_slug", ""),
                    "position_count": 1 if entry else 0,
                    "simulated_buy_usd": entry.get("intended", 0.0),
                    "simulated_entry_spent_usd": entry.get("spent", 0.0),
                    "unfilled_entry_usd": entry.get("unfilled", 0.0),
                    "avg_entry_vwap": safe_div(entry.get("entry_value", 0.0), entry.get("shares", 0.0)),
                    "avg_exit_vwap": safe_div(exit_agg.get("exit_value", 0.0), exit_agg.get("shares", 0.0)),
                    "gross_pnl": fnum(settlement.get("gross_pnl"), math.nan),
                    "net_pnl": fnum(settlement.get("net_pnl"), math.nan),
                    "triggered_take_profit": bool(exit_agg.get("triggered", 0)),
                    "incomplete_take_profit": bool(exit_agg.get("incomplete", 0)),
                    "settled": bool(settlement),
                }
            )
    return rows


def concentration(values: list[float], k: int) -> float:
    total = sum(values)
    positives = sorted([v for v in values if v > 0], reverse=True)
    return safe_div(sum(positives[:k]), total) if total > 0 else math.nan


def strategy_summary(event_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    hold_by_event = {
        row["event_id"]: fnum(row.get("net_pnl"), math.nan)
        for row in event_rows
        if row["strategy_id"] == "hold_to_settlement" and str(row.get("settled")) == "True"
    }
    for strategy_id in STRATEGY_IDS:
        rows = [r for r in event_rows if r["strategy_id"] == strategy_id]
        settled = [r for r in rows if str(r.get("settled")) == "True"]
        pnls = [fnum(r.get("net_pnl"), math.nan) for r in settled if math.isfinite(fnum(r.get("net_pnl"), math.nan))]
        entry_spent = sum(fnum(r.get("simulated_entry_spent_usd")) for r in rows)
        unfilled = sum(fnum(r.get("unfilled_entry_usd")) for r in rows)
        delta_vs_hold = sum(
            fnum(r.get("net_pnl")) - hold_by_event.get(r["event_id"], 0.0)
            for r in settled
            if r["event_id"] in hold_by_event
        )
        out.append(
            {
                "strategy_id": strategy_id,
                "events": len({r["event_id"] for r in rows}),
                "positions": sum(1 for r in rows if fnum(r.get("position_count")) > 0),
                "entry_spent": entry_spent,
                "unfilled_rate": safe_div(unfilled, entry_spent + unfilled),
                "settled_events": len(settled),
                "net_pnl": sum(pnls) if pnls else math.nan,
                "delta_vs_hold": delta_vs_hold,
                "profit_event_rate": safe_div(sum(1 for p in pnls if p > 0), len(pnls)),
                "median_event_pnl": statistics.median(pnls) if pnls else math.nan,
                "max_event_loss": min(pnls) if pnls else math.nan,
                "top1_profit_share": concentration(pnls, 1),
                "top5_profit_share": concentration(pnls, 5),
                "triggered_events": sum(1 for r in rows if str(r.get("triggered_take_profit")) == "True"),
                "incomplete_take_profit_events": sum(1 for r in rows if str(r.get("incomplete_take_profit")) == "True"),
            }
        )
    return out


def markdown_table(rows: list[dict[str, Any]], cols: list[tuple[str, str]]) -> str:
    if not rows:
        return "_No rows._"
    out = [
        "| " + " | ".join(label for label, _ in cols) + " |",
        "| " + " | ".join("---" for _ in cols) + " |",
    ]
    for row in rows:
        out.append("| " + " | ".join(str(row.get(key, "")) for _, key in cols) + " |")
    return "\n".join(out)


def generate_status(root: Path) -> dict[str, Any]:
    formal_dir = data_dir_for(root, "formal")
    demo_dir = data_dir_for(root, "demo")
    formal_rows = aggregate_event_results(formal_dir, "formal") if formal_dir.exists() else []
    demo_rows = aggregate_event_results(demo_dir, "demo") if demo_dir.exists() else []
    write_csv(formal_dir / "event_results.csv", formal_rows, EVENT_RESULT_FIELDS)
    write_csv(demo_dir / "event_results.csv", demo_rows, EVENT_RESULT_FIELDS)
    formal_summary = strategy_summary(formal_rows)
    demo_summary = strategy_summary(demo_rows)

    report = [
        "# FORWARD_SIMULATION_V5_CURRENT_STATUS",
        "",
        f"Generated at: {now_utc()}",
        "",
        "## Formal Sample",
        "",
        "正式样本目前只包含系统启用后记录的数据；demo 数据在独立目录，不进入正式统计。",
        "",
        markdown_table(
            [
                {
                    "strategy": r["strategy_id"],
                    "events": r["events"],
                    "spent": fmt_money(r["entry_spent"]),
                    "unfilled": fmt_pct(r["unfilled_rate"]),
                    "settled": r["settled_events"],
                    "pnl": fmt_money(r["net_pnl"]),
                    "delta": fmt_money(r["delta_vs_hold"]),
                }
                for r in formal_summary
            ],
            [("Strategy", "strategy"), ("Events", "events"), ("Entry Spent", "spent"), ("Unfilled", "unfilled"), ("Settled", "settled"), ("Net PnL", "pnl"), ("Delta vs Hold", "delta")],
        ),
        "",
        "## Demo Walkthrough",
        "",
        markdown_table(
            [
                {
                    "strategy": r["strategy_id"],
                    "events": r["events"],
                    "spent": fmt_money(r["entry_spent"]),
                    "unfilled": fmt_pct(r["unfilled_rate"]),
                    "settled": r["settled_events"],
                    "pnl": fmt_money(r["net_pnl"]),
                    "triggered": r["triggered_events"],
                    "incomplete": r["incomplete_take_profit_events"],
                }
                for r in demo_summary
            ],
            [("Strategy", "strategy"), ("Events", "events"), ("Entry Spent", "spent"), ("Unfilled", "unfilled"), ("Settled", "settled"), ("Net PnL", "pnl"), ("TP Events", "triggered"), ("Partial TP", "incomplete")],
        ),
        "",
        "## Health Checks",
        "",
        f"- Formal rows: {len(formal_rows)} event-strategy rows.",
        f"- Demo rows: {len(demo_rows)} event-strategy rows.",
        "- No real trading or wallet connection is implemented in this system.",
    ]
    report_path = root / "reports/FORWARD_SIMULATION_V5_CURRENT_STATUS.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(report) + "\n", encoding="utf-8")
    return {"formal_rows": len(formal_rows), "demo_rows": len(demo_rows), "report_path": str(report_path)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    args = parser.parse_args()
    print(json.dumps(generate_status(Path(args.root)), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
