#!/usr/bin/env python3
"""Reports for v5.1.2 public live-integration acceptance."""

from __future__ import annotations

import argparse
import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
LIVE_DIR = Path("data/forward_v5_1_2/live_integration")
REPORTS = Path("reports")


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def fnum(value: Any, default: float = math.nan) -> float:
    try:
        if value in ("", None):
            return default
        x = float(value)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).lower() in {"true", "1", "yes"}


def md_table(rows: list[dict[str, Any]], columns: list[tuple[str, str]], limit: int | None = None) -> str:
    if not rows:
        return "_No rows._"
    data = rows[:limit] if limit else rows
    lines = [
        "| " + " | ".join(label for label, _ in columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in data:
        lines.append("| " + " | ".join(str(row.get(key, "")) for _, key in columns) + " |")
    return "\n".join(lines)


def load_live(root: Path) -> dict[str, Any]:
    live = root / LIVE_DIR
    return {
        "live": live,
        "summary": read_json(live / "live_integration_summary.json", {}),
        "selected": read_json(live / "selected_markets.json", []),
        "token_rows": read_csv(live / "token_mapping_validation.csv"),
        "snapshots": read_jsonl(live / "orderbook_snapshots.jsonl"),
        "vwap_rows": read_csv(live / "live_vwap_validation.csv"),
        "fee_rows": read_csv(live / "fee_validation.csv"),
        "status_rows": read_jsonl(live / "market_status_snapshots.jsonl"),
        "isolation": read_json(live / "formal_isolation_proof.json", {}),
        "scan": read_json(live / "read_only_security_scan.json", {}),
        "failure": read_json(live / "network_failure_probe.json", {}),
    }


def summarize_live(data: dict[str, Any]) -> dict[str, Any]:
    snapshots = data["snapshots"]
    token_rows = data["token_rows"]
    vwap_rows = data["vwap_rows"]
    fee_rows = data["fee_rows"]
    buy_sizes = {str(x) for x in [1, 5, 10, 25, 50]}
    has_buy_sizes = buy_sizes.issubset({str(float(r["intended_usd_or_shares"])).rstrip("0").rstrip(".") for r in vwap_rows if r.get("action") == "buy"})
    sell_ok = any(r.get("action") == "sell" and fnum(r.get("filled_shares"), 0) > 0 and boolish(r.get("calculation_valid")) for r in vwap_rows)
    no_seller_depth = any(r.get("action") == "sell" and fnum(r.get("filled_shares"), 0) == 0 and boolish(r.get("calculation_valid")) for r in vwap_rows)
    partial = any(not boolish(r.get("fully_filled")) and boolish(r.get("calculation_valid")) for r in vwap_rows)
    empty_books = sum(1 for s in snapshots if boolish(s.get("empty")))
    mapping_ok = bool(token_rows) and all(boolish(r.get("mapping_valid")) for r in token_rows)
    direction_ok = bool(snapshots) and all(
        (s.get("best_bid") is None or 0 <= fnum(s.get("best_bid")) <= 1)
        and (s.get("best_ask") is None or 0 <= fnum(s.get("best_ask")) <= 1)
        and not (s.get("best_bid") is not None and s.get("best_ask") is not None and fnum(s.get("best_bid")) > fnum(s.get("best_ask")))
        for s in snapshots
    )
    fee_ok = bool(fee_rows) and all(r.get("fee_status") in {"official", "disabled", "unknown", "fallback"} for r in fee_rows) and not any(r.get("fee_status") == "unknown" and r.get("official_fee") in {"0", "0.0"} for r in fee_rows)
    fee_official_or_disabled = bool(fee_rows) and all(r.get("fee_status") in {"official", "disabled"} for r in fee_rows)
    latest_status = {}
    selected_slugs = {row.get("market_slug", "") for row in data["selected"]}
    for row in data["status_rows"]:
        if selected_slugs and row.get("market_slug", "") not in selected_slugs:
            continue
        latest_status[row.get("market_slug", "")] = row
    unresolved = sum(1 for r in latest_status.values() if r.get("market_status") != "resolved")
    manual_snapshots = len({r.get("snapshot_id") for r in vwap_rows if r.get("manual_check") == "yes"})
    endpoints = data["scan"].get("actual_endpoints") or []
    only_get = all(e.get("method") == "GET" for e in endpoints)
    critical_ok = all([
        bool(data["selected"]),
        bool(snapshots),
        mapping_ok,
        direction_ok,
        has_buy_sizes,
        sell_ok,
        fee_ok,
        boolish(data["isolation"].get("ok")),
        boolish(data["scan"].get("ok")),
        boolish(data["failure"].get("ok")),
        only_get,
    ])
    if not critical_ok:
        conclusion = "BLOCKED"
    elif unresolved or empty_books or no_seller_depth or not fee_official_or_disabled:
        conclusion = "PASS_WITH_MINOR_LIMITATIONS"
    else:
        conclusion = "PASS_FOR_FORMAL_START"
    return {
        "conclusion": conclusion,
        "market_count": len({r.get("market_slug") for r in data["selected"]}),
        "token_count": len({r.get("token_id") for r in data["selected"]}),
        "snapshot_count": int(data["summary"].get("snapshot_count") or len(snapshots)),
        "duration_seconds": fnum(data["summary"].get("duration_seconds"), 0),
        "mapping_ok": mapping_ok,
        "direction_ok": direction_ok,
        "has_buy_sizes": has_buy_sizes,
        "sell_ok": sell_ok,
        "partial_fill_seen": partial,
        "empty_books": empty_books,
        "fee_ok": fee_ok,
        "fee_official_or_disabled": fee_official_or_disabled,
        "status_ok": bool(data["status_rows"]),
        "unresolved_markets": unresolved,
        "failure_ok": boolish(data["failure"].get("ok")),
        "isolation_ok": boolish(data["isolation"].get("ok")),
        "scan_ok": boolish(data["scan"].get("ok")),
        "only_get": only_get,
        "manual_snapshot_checks": manual_snapshots,
    }


def write_report(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def generate(root: Path) -> dict[str, Any]:
    data = load_live(root)
    summary = summarize_live(data)
    reports_dir = root / REPORTS
    generated = now_utc()

    endpoints = data["scan"].get("actual_endpoints") or []
    endpoint_rows = [
        {"method": e.get("method"), "url": e.get("url"), "status_code": e.get("status_code"), "latency_ms": round(fnum(e.get("latency_ms"), 0), 1)}
        for e in endpoints
    ]
    selected_rows = [
        {
            "event_title": r.get("event_title", ""),
            "market_slug": r.get("market_slug", ""),
            "outcome_label": r.get("outcome_label", ""),
            "active": r.get("active"),
            "closed": r.get("closed"),
            "resolved": r.get("resolved"),
            "fees_enabled": r.get("fees_enabled"),
        }
        for r in data["selected"]
    ]
    selected_slugs = {r.get("market_slug", "") for r in data["selected"]}
    latest_status_rows = {}
    for row in data["status_rows"]:
        if selected_slugs and row.get("market_slug", "") not in selected_slugs:
            continue
        latest_status_rows[row.get("market_slug", "")] = row
    current_status_rows = list(latest_status_rows.values())
    fee_statuses = sorted({r.get("fee_status", "") for r in data["fee_rows"]})
    fee_params = sorted({str(r.get("fee_rate_parameter", "")) for r in data["fee_rows"] if r.get("fee_rate_parameter") not in {"", None}})

    write_report(
        reports_dir / "FORWARD_SIMULATION_V5_1_2_OFFICIAL_API_CONTRACT.md",
        [
            "# FORWARD_SIMULATION_V5_1_2_OFFICIAL_API_CONTRACT",
            "",
            f"Generated at: {generated}",
            "",
            "## Official Sources Checked",
            "",
            "- Polymarket Fetching Markets: https://docs.polymarket.com/market-data/fetching-markets",
            "- Polymarket Search markets, events, and profiles: https://docs.polymarket.com/api-reference/search/search-markets-events-and-profiles",
            "- Polymarket Get market by slug: https://docs.polymarket.com/api-reference/markets/get-market-by-slug",
            "- Polymarket Get order book: https://docs.polymarket.com/api-reference/market-data/get-order-book",
            "- Polymarket Public Methods: https://docs.polymarket.com/trading/clients/public",
            "- Polymarket Fees: https://docs.polymarket.com/trading/fees",
            "- Polymarket Resolution: https://docs.polymarket.com/concepts/resolution",
            "",
            "## Contract Used",
            "",
            "- Market discovery: `GET /public-search?q=...&events_status=active&limit_per_type=10&keep_closed_markets=0`, with `/events?active=true&closed=false&limit=100` as fallback.",
            "- Market detail: `GET /markets/slug/{slug}`; key fields include `conditionId`, `slug`, `outcomes`, `clobTokenIds`, `active`, `closed`, `feesEnabled`, and `feeSchedule`.",
            "- CLOB market parameters: `GET /clob-markets/{condition_id}`; key fields include token mapping `t`, `mos`, `mts`, `mbf`, `tbf`, and `fd`.",
            "- Order book: `GET /book?token_id=...`; key fields include `market`, `asset_id`, `timestamp`, `hash`, `bids`, `asks`, `min_order_size`, `tick_size`, `neg_risk`, and `last_trade_price`.",
            "- Direction: bids are sorted high-to-low and represent executable sell-side liquidity for us; asks are sorted low-to-high and represent executable buy-side liquidity for us.",
            "- Fee formula: `fee = shares * fee_rate * price * (1 - price)`, rounded to 5 decimals in this acceptance harness; unknown fee is not treated as zero.",
            "- Settlement: only official resolved market state and winning outcome create settlement evidence; visible weather observations alone are not settlement.",
            "",
            "## Actual Endpoints",
            "",
            md_table(endpoint_rows, [("Method", "method"), ("Status", "status_code"), ("Latency ms", "latency_ms"), ("URL", "url")], limit=80),
            "",
            "## Real Response Differences",
            "",
            "- `public-search` can return active events whose child markets are already closed or resolved, so v5.1.2 filters child markets by `active=True`, `closed=False`, and unresolved status before sampling.",
            "- `/events?active=true&closed=false&limit=100` can return a large payload and may exceed short acceptance timeouts; v5.1.2 uses search first and records fallback failures instead of silently accepting partial JSON.",
        ],
    )

    write_report(
        reports_dir / "FORWARD_SIMULATION_V5_1_2_FEE_VALIDATION.md",
        [
            "# FORWARD_SIMULATION_V5_1_2_FEE_VALIDATION",
            "",
            f"Generated at: {generated}",
            "",
            f"- Fee statuses observed: {', '.join(fee_statuses) if fee_statuses else 'none'}",
            f"- Fee rate parameters observed: {', '.join(fee_params) if fee_params else 'none'}",
            "- Official gross and official-net values are both preserved; fallback fee is separate and is not substituted for official values.",
            "- When fees are disabled, official fee is 0. When fee parameters are unavailable, status remains `unknown` and official fee stays blank.",
            "",
            md_table(data["fee_rows"], [("Market", "market_slug"), ("Enabled", "fees_enabled"), ("Source", "fee_parameter_source"), ("Rate", "fee_rate_parameter"), ("Formula", "fee_formula_version"), ("Gross", "gross_notional"), ("Official fee", "official_fee"), ("Status", "fee_status"), ("Official net", "net_proceeds_or_cost")], limit=40),
        ],
    )

    write_report(
        reports_dir / "FORWARD_SIMULATION_V5_1_2_SETTLEMENT_WORKFLOW.md",
        [
            "# FORWARD_SIMULATION_V5_1_2_SETTLEMENT_WORKFLOW",
            "",
            f"Generated at: {generated}",
            "",
            "- This acceptance run does not resolve live weather results from weather observations.",
            "- A market is settlement-eligible only when official market status is resolved and a winning outcome or token value can be tied back to the market response.",
            "- Active or closed-but-unresolved markets produce status evidence only, not final settlement rows.",
            "",
            md_table(current_status_rows, [("Market", "market_slug"), ("Status", "market_status"), ("Resolution", "resolution_status"), ("Winner", "winning_outcome"), ("Token value", "token_settlement_value"), ("Evidence valid", "evidence_valid")], limit=40),
        ],
    )

    write_report(
        reports_dir / "FORWARD_SIMULATION_V5_1_2_LIVE_INTEGRATION_AUDIT.md",
        [
            "# FORWARD_SIMULATION_V5_1_2_LIVE_INTEGRATION_AUDIT",
            "",
            f"Generated at: {generated}",
            "",
            f"Acceptance conclusion: **{summary['conclusion']}**",
            "",
            "## Scope",
            "",
            "- Public read-only API integration only.",
            "- No formal sample start, no formal prediction signal, no live trade action.",
            "- Demo data is isolated under `data/forward_v5_1_2/live_integration/`.",
            "",
            "## Selected Markets",
            "",
            md_table(selected_rows, [("Event", "event_title"), ("Market", "market_slug"), ("Outcome", "outcome_label"), ("Active", "active"), ("Closed", "closed"), ("Resolved", "resolved"), ("Fees", "fees_enabled")], limit=20),
            "",
            "## Checks",
            "",
            f"- Token mapping valid: {summary['mapping_ok']}",
            f"- Bid/ask direction valid: {summary['direction_ok']}",
            f"- 1/5/10/25/50 USD buy VWAP rows present: {summary['has_buy_sizes']}",
            f"- Sell VWAP has executable bid depth in at least one token: {summary['sell_ok']}",
            f"- Partial fill observed: {summary['partial_fill_seen']}",
            f"- Empty order books: {summary['empty_books']}",
            f"- Manual-program VWAP check snapshots marked: {summary['manual_snapshot_checks']}",
            f"- Fee handling valid: {summary['fee_ok']}",
            f"- Network recovery probe valid: {summary['failure_ok']}",
            f"- Formal isolation valid: {summary['isolation_ok']}",
            f"- Read-only static scan valid: {summary['scan_ok']}",
            f"- All recorded real endpoints used GET: {summary['only_get']}",
        ],
    )

    write_report(
        reports_dir / "FORWARD_SIMULATION_V5_1_2_OPERATIONS.md",
        [
            "# FORWARD_SIMULATION_V5_1_2_OPERATIONS",
            "",
            "## Purpose",
            "",
            "v5.1.2 is a short, foreground, public-data acceptance harness. It is not the formal forward simulation.",
            "",
            "## Commands",
            "",
            "```bash",
            "python3 src/forward_simulation_v5_1_2.py --root . --config config/forward_simulation_v5_1_2.yaml discover",
            "python3 src/forward_simulation_v5_1_2.py --root . --config config/forward_simulation_v5_1_2.yaml sample --iterations 15 --interval-seconds 60",
            "python3 src/forward_reporting_v5_1_2.py --root .",
            "```",
            "",
            "Stop a sampling run with Ctrl+C. A stopped run leaves already written live-integration snapshots available for audit.",
            "",
            "Only the live-integration directory is written. Formal directories remain off limits for this acceptance step.",
        ],
    )

    write_report(
        reports_dir / "FORWARD_SIMULATION_V5_1_2_STATUS.md",
        [
            "# FORWARD_SIMULATION_V5_1_2_STATUS",
            "",
            f"Generated at: {generated}",
            "",
            f"- Conclusion: {summary['conclusion']}",
            f"- Tested markets: {summary['market_count']}",
            f"- Tested tokens: {summary['token_count']}",
            f"- Real orderbook duration seconds: {round(summary['duration_seconds'], 1)}",
            f"- Snapshot count: {summary['snapshot_count']}",
            f"- Token mapping passed: {summary['mapping_ok']}",
            f"- Bid/ask direction passed: {summary['direction_ok']}",
            f"- Fee parameters official or disabled: {summary['fee_official_or_disabled']}",
            f"- Formal isolation passed: {summary['isolation_ok']}",
            f"- Static read-only scan passed: {summary['scan_ok']}",
            f"- Network failure probe passed: {summary['failure_ok']}",
            f"- Unresolved current markets: {summary['unresolved_markets']}",
            "",
            "## Files",
            "",
            "- `selected_markets.json`",
            "- `token_mapping_validation.csv`",
            "- `orderbook_snapshots.jsonl`",
            "- `live_vwap_validation.csv`",
            "- `fee_validation.csv`",
            "- `market_status_snapshots.jsonl`",
            "- `formal_isolation_proof.json`",
            "- `read_only_security_scan.json`",
            "- `network_failure_probe.json`",
        ],
    )

    return {"generated_at_utc": generated, **summary}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    args = parser.parse_args()
    result = generate(Path(args.root).resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
