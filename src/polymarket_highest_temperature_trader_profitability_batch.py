#!/usr/bin/env python3
"""Phase 2 batch orchestration around the frozen Discovery candidate pool.

This module deliberately contains no PnL calculations.  It freezes the
Discovery input, runs the existing official Hybrid Profitability core in
small auditable batches, and assembles result artifacts without changing the
core reconciliation semantics.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .polymarket_highest_temperature_trader_pattern_v1 import (
    PublicGetClient,
    normalize_wallets,
    sha256_file,
    write_csv,
    write_json,
)
from .polymarket_highest_temperature_trader_profitability import (
    PNL_SOURCE,
    collect_profitability_evidence,
    load_profitability_evidence,
    run_profitability_analysis,
    save_profitability_evidence,
)

CITY = "beijing"
DATE_FROM = "2026-03-01"
DATE_TO = "2026-08-10"
BATCH_SIZE = 5
WALLET_RE = re.compile(r"^0x[a-f0-9]{40}$")
CONTROL_WALLETS = {
    "A": "0x8fbd7cf5f806f563080864694415829f7229a959",
    "B": "0x7c63520c2ca9b336af0c205b9ccf68217bb393d4",
}
CONTROL_EXPECTED_PNL = {"A": 5613.1946, "B": 3180.8628}

RESULT_FIELDS = [
    "batch_number", "discovery_priority_rank", "wallet", "selection_channel",
    "run_status", "request_status", "profitability_status", "status_reason",
    "evidence_status", "evidence_complete_for_resolved_subset",
    "total_settled_pnl_usd", "settled_market_weather_days", "profitable_days",
    "loss_days", "zero_pnl_days", "profitable_day_rate", "average_daily_pnl",
    "median_daily_pnl", "max_daily_profit", "max_daily_loss", "longest_loss_streak",
    "profitability_stability", "positive_month_count", "negative_month_count",
    "monthly_pnl", "profit_concentration", "top1_profit_days_share",
    "top3_profit_days_share", "top10_profit_days_share", "settled_pnl_rank",
    "result_class",
]


class BatchProfitabilityError(RuntimeError):
    """Raised when Phase 2 cannot safely continue."""


class ControlGroupFailure(BatchProfitabilityError):
    """Raised when either known control does not reproduce its reference PnL."""


def _wallet_set_sha256(wallets: Iterable[str]) -> str:
    canonical = "\n".join(sorted(set(wallets))) + "\n"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_frozen_candidate_pool(path: Path) -> dict[str, Any]:
    """Read and validate exactly the committed Phase 1 candidate pool."""
    path = path.resolve()
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != 30:
        raise BatchProfitabilityError(f"CANDIDATE_POOL_SIZE={len(rows)}; expected 30")
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for expected_rank, row in enumerate(rows, start=1):
        try:
            rank = int(row.get("discovery_priority_rank") or 0)
        except ValueError as exc:
            raise BatchProfitabilityError("candidate pool contains a non-integer rank") from exc
        wallet = str(row.get("wallet") or "").strip().lower()
        if rank != expected_rank:
            raise BatchProfitabilityError("candidate pool ranks are not 1..30")
        if not WALLET_RE.fullmatch(wallet):
            raise BatchProfitabilityError(f"invalid candidate wallet: {wallet!r}")
        if wallet in seen:
            raise BatchProfitabilityError(f"duplicate candidate wallet: {wallet}")
        seen.add(wallet)
        candidates.append({
            "discovery_priority_rank": rank,
            "wallet": wallet,
            "selection_channel": str(row.get("selection_channel") or ""),
        })
    if seen.intersection(CONTROL_WALLETS.values()):
        raise BatchProfitabilityError("A/B benchmark wallet entered the frozen candidate pool")
    normalize_wallets(seen)
    return {
        "path": path,
        "candidates": candidates,
        "wallets": [row["wallet"] for row in candidates],
        "candidate_wallet_count": len(candidates),
        "candidate_wallet_set_sha256": _wallet_set_sha256(seen),
        "candidate_pool_sha256": sha256_file(path),
    }


def _resolved_subset_status(
    status: str,
    audit: Iterable[dict[str, Any]],
) -> tuple[str, str, str, bool]:
    rows = list(audit)
    resolved = [row for row in rows if row.get("settlement_status") == "RESOLVED"]
    unresolved = [row for row in rows if row.get("settlement_status") != "RESOLVED"]
    resolved_complete = bool(resolved) and all(
        row.get("request_status") == "COMPLETE" for row in resolved
    )
    resolved_failed = any(
        row.get("request_status") not in {"COMPLETE", "EXCLUDED_NOT_RESOLVED"}
        for row in resolved
    )
    historical_only = bool(unresolved) and all(
        row.get("request_status") == "EXCLUDED_NOT_RESOLVED" for row in unresolved
    )
    if status == "BLOCKED" or resolved_failed:
        return "BLOCKED_INCOMPLETE_EVIDENCE", "FAILED", "NO", False
    if resolved_complete and historical_only:
        return "PARTIAL_HISTORICAL_UNRESOLVED", "PARTIAL", "YES", True
    if resolved_complete and not unresolved:
        return "COMPLETE_RESOLVED_SCOPE", "COMPLETE", "YES", True
    return "INCOMPLETE", "FAILED", "NO", False


def result_class(total_pnl: Any, settled_days: Any, evidence_complete: bool, status: str) -> str:
    """Classify objective outcome without calling a wallet an expert."""
    if not evidence_complete or status == "BLOCKED":
        return "BLOCKED_INCOMPLETE_EVIDENCE"
    try:
        days = int(settled_days or 0)
    except (TypeError, ValueError):
        days = 0
    if days <= 0:
        return "NO_SETTLED_SAMPLE"
    if total_pnl is None:
        return "BLOCKED_INCOMPLETE_EVIDENCE"
    value = float(total_pnl)
    if value > 0:
        return "POSITIVE_PNL"
    if value < 0:
        return "NEGATIVE_PNL"
    return "ZERO_PNL"


def result_from_summary(
    candidate: dict[str, Any],
    summary: dict[str, Any],
    audit: Iterable[dict[str, Any]],
    *,
    batch_number: int,
) -> dict[str, Any]:
    status = str(summary.get("PROFITABILITY_STATUS") or "BLOCKED")
    reasons = [str(value) for value in summary.get("PROFITABILITY_STATUS_REASONS") or []]
    evidence_status, request_status, evidence_flag, evidence_complete = _resolved_subset_status(status, audit)
    if evidence_status == "PARTIAL_HISTORICAL_UNRESOLVED" and "KNOWN_HISTORICAL_UNRESOLVED_MARKET" not in reasons:
        reasons.append("KNOWN_HISTORICAL_UNRESOLVED_MARKET")
    settled_days = summary.get("SETTLED_MARKET_WEATHER_DAYS", 0)
    return {
        "batch_number": batch_number,
        "discovery_priority_rank": candidate.get("discovery_priority_rank"),
        "wallet": candidate["wallet"],
        "selection_channel": candidate.get("selection_channel", ""),
        "run_status": "COMPLETED",
        "request_status": request_status,
        "profitability_status": status,
        "status_reason": ";".join(dict.fromkeys(reasons)) or "NONE",
        "evidence_status": evidence_status,
        "evidence_complete_for_resolved_subset": "YES" if evidence_complete else "NO",
        "total_settled_pnl_usd": summary.get("TOTAL_SETTLED_PNL_USD") if evidence_complete else None,
        "settled_market_weather_days": settled_days,
        "profitable_days": summary.get("PROFITABLE_DAYS", 0),
        "loss_days": summary.get("LOSS_DAYS", 0),
        "zero_pnl_days": summary.get("ZERO_PNL_DAYS", 0),
        "profitable_day_rate": summary.get("PROFITABLE_DAY_RATE"),
        "average_daily_pnl": summary.get("AVERAGE_DAILY_PNL"),
        "median_daily_pnl": summary.get("MEDIAN_DAILY_PNL"),
        "max_daily_profit": summary.get("MAX_DAILY_PROFIT"),
        "max_daily_loss": summary.get("MAX_DAILY_LOSS"),
        "longest_loss_streak": summary.get("LONGEST_LOSS_STREAK"),
        "profitability_stability": summary.get("PROFITABILITY_STABILITY"),
        "positive_month_count": summary.get("MONTHS_WITH_POSITIVE_PNL", 0),
        "negative_month_count": summary.get("MONTHS_WITH_NEGATIVE_PNL", 0),
        "monthly_pnl": summary.get("MONTHLY_PNL", {}),
        "profit_concentration": "FIELD_NOT_AVAILABLE_IN_FORMAL_MODEL",
        "top1_profit_days_share": summary.get("TOP1_PROFIT_DAYS_SHARE"),
        "top3_profit_days_share": summary.get("TOP3_PROFIT_DAYS_SHARE"),
        "top10_profit_days_share": summary.get("TOP10_PROFIT_DAYS_SHARE"),
        "settled_pnl_rank": None,
        "result_class": result_class(
            summary.get("TOTAL_SETTLED_PNL_USD"), settled_days,
            evidence_complete, status,
        ),
    }


def failure_result(
    candidate: dict[str, Any],
    reason: str,
    *,
    batch_number: int,
) -> dict[str, Any]:
    return {
        "batch_number": batch_number,
        "discovery_priority_rank": candidate.get("discovery_priority_rank"),
        "wallet": candidate["wallet"],
        "selection_channel": candidate.get("selection_channel", ""),
        "run_status": "FAILED",
        "request_status": "FAILED",
        "profitability_status": "BLOCKED",
        "status_reason": reason,
        "evidence_status": "BLOCKED_INCOMPLETE_EVIDENCE",
        "evidence_complete_for_resolved_subset": "NO",
        "total_settled_pnl_usd": None,
        "settled_market_weather_days": 0,
        "profitable_days": 0,
        "loss_days": 0,
        "zero_pnl_days": 0,
        "profitable_day_rate": None,
        "average_daily_pnl": None,
        "median_daily_pnl": None,
        "max_daily_profit": None,
        "max_daily_loss": None,
        "longest_loss_streak": None,
        "profitability_stability": "INSUFFICIENT_DATA",
        "positive_month_count": 0,
        "negative_month_count": 0,
        "monthly_pnl": {},
        "profit_concentration": "FIELD_NOT_AVAILABLE_IN_FORMAL_MODEL",
        "top1_profit_days_share": None,
        "top3_profit_days_share": None,
        "top10_profit_days_share": None,
        "settled_pnl_rank": None,
        "result_class": "BLOCKED_INCOMPLETE_EVIDENCE",
    }


def rank_settled_results(results: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = [dict(row) for row in results]
    eligible = [
        row for row in rows
        if row.get("evidence_complete_for_resolved_subset") == "YES"
        and row.get("total_settled_pnl_usd") is not None
    ]
    eligible.sort(key=lambda row: (
        -float(row.get("total_settled_pnl_usd") or 0),
        -int(row.get("settled_market_weather_days") or 0),
        str(row.get("wallet") or ""),
    ))
    for rank, row in enumerate(eligible, start=1):
        row["settled_pnl_rank"] = rank
    by_wallet = {row["wallet"]: row for row in eligible}
    return [by_wallet.get(row["wallet"], row) for row in rows]


def control_match(result: dict[str, Any], expected_pnl: float, tolerance: float = 0.01) -> bool:
    actual = result.get("total_settled_pnl_usd")
    return (
        result.get("evidence_complete_for_resolved_subset") == "YES"
        and actual is not None
        and abs(float(actual) - expected_pnl) <= tolerance
    )


def require_controls_match(control_results: dict[str, dict[str, Any]]) -> dict[str, str]:
    matches = {
        label: "YES" if control_match(control_results[label], CONTROL_EXPECTED_PNL[label]) else "NO"
        for label in CONTROL_WALLETS
    }
    if any(value != "YES" for value in matches.values()):
        raise ControlGroupFailure("A/B control PnL did not match reference")
    return matches


def _checkpoint_payload(
    candidate_info: dict[str, Any],
    completed_batches: list[int],
    results: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": "polymarket_highest_temperature_trader_profitability_batch_checkpoint_v1",
        "candidate_wallet_set_sha256": candidate_info["candidate_wallet_set_sha256"],
        "completed_batches": sorted(set(completed_batches)),
        "results": list(results),
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def run_checkpointed_batches(
    candidates: list[dict[str, Any]],
    *,
    batch_size: int,
    checkpoint_path: Path,
    candidate_info: dict[str, Any],
    runner: Callable[[list[dict[str, Any]], int], list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Run deterministic batches, resuming only from complete checkpoints."""
    checkpoint_path = checkpoint_path.resolve()
    existing: dict[str, dict[str, Any]] = {}
    completed_batches: list[int] = []
    if checkpoint_path.is_file():
        payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if payload.get("candidate_wallet_set_sha256") != candidate_info["candidate_wallet_set_sha256"]:
            raise BatchProfitabilityError("checkpoint candidate wallet set mismatch")
        existing = {str(row["wallet"]): row for row in payload.get("results") or []}
        completed_batches = [int(value) for value in payload.get("completed_batches") or []]
    batches = [candidates[index:index + batch_size] for index in range(0, len(candidates), batch_size)]
    for batch_number, batch in enumerate(batches, start=1):
        if all(candidate["wallet"] in existing for candidate in batch):
            continue
        returned = runner(batch, batch_number)
        returned_wallets = [str(row.get("wallet") or "") for row in returned]
        expected_wallets = [candidate["wallet"] for candidate in batch]
        if returned_wallets != expected_wallets:
            raise BatchProfitabilityError(f"batch {batch_number} returned a different wallet sequence")
        if len(set(returned_wallets)) != len(returned_wallets):
            raise BatchProfitabilityError(f"batch {batch_number} returned duplicate wallets")
        existing.update({row["wallet"]: row for row in returned})
        completed_batches.append(batch_number)
        write_json(
            checkpoint_path,
            _checkpoint_payload(candidate_info, completed_batches, [
                existing[candidate["wallet"]] for candidate in candidates
                if candidate["wallet"] in existing
            ]),
        )
    return [existing[candidate["wallet"]] for candidate in candidates if candidate["wallet"] in existing]


def _annotate_requests(requests: Iterable[dict[str, Any]], *, group: str, batch_number: int | None = None) -> list[dict[str, Any]]:
    return [
        {**row, "phase2_group": group, "batch_number": batch_number}
        for row in requests
    ]


def _run_wallet_group(
    candidates: list[dict[str, Any]],
    *,
    target_markets: list[dict[str, Any]],
    date_from: date,
    date_to: date,
    group_root: Path,
    group: str,
    batch_number: int | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    evidence_by_wallet: dict[str, dict[str, Any]] = {}
    audits_by_wallet: dict[str, list[dict[str, Any]]] = {}
    requests: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    def run_one(candidate: dict[str, Any]) -> dict[str, Any]:
        wallet = candidate["wallet"]
        evidence_root = group_root / "_profitability_public_evidence" / wallet
        client = PublicGetClient(evidence_root)
        try:
            positions, audit, meta = collect_profitability_evidence(
                client, wallet, target_markets, date_from, date_to, [CITY],
                observed_fills=None,
            )
            meta["api_request_count"] = len(client.requests)
            meta["api_request_failure_count"] = sum(
                row.get("success") is not True for row in client.requests
            )
            evidence_by_wallet[wallet] = {
                "market_positions": positions,
                "closed_positions": meta.get("_closed_positions") or [],
                "profitability_event_audit": audit,
                "profitability_collection_meta": meta,
            }
            audits_by_wallet[wallet] = audit
            save_profitability_evidence(
                evidence_root, wallet, date_from, date_to, [CITY],
                positions, audit, meta, client.requests,
            )
            return {
                "wallet": wallet,
                "candidate": candidate,
                "evidence": {
                    "market_positions": positions,
                    "closed_positions": meta.get("_closed_positions") or [],
                    "profitability_event_audit": audit,
                    "profitability_collection_meta": meta,
                },
                "audit": audit,
                "requests": _annotate_requests(client.requests, group=group, batch_number=batch_number),
            }
        except Exception as exc:  # one wallet is blocked, never silently zeroed
            return {
                "wallet": wallet,
                "candidate": candidate,
                "failure": failure_result(
                    candidate, f"{type(exc).__name__}:{exc}",
                    batch_number=batch_number or 0,
                ),
                "requests": _annotate_requests(client.requests, group=group, batch_number=batch_number),
            }

    worker_count = min(len(candidates), BATCH_SIZE)
    with ThreadPoolExecutor(max_workers=max(1, worker_count)) as pool:
        futures = [pool.submit(run_one, candidate) for candidate in candidates]
        completed = [future.result() for future in as_completed(futures)]
    for item in completed:
        wallet = item["wallet"]
        requests.extend(item.get("requests") or [])
        if item.get("failure") is not None:
            failures.append(item["failure"])
            continue
        evidence_by_wallet[wallet] = item["evidence"]
        audits_by_wallet[wallet] = item["audit"]
    if not evidence_by_wallet and failures:
        raise BatchProfitabilityError(f"all wallets failed in {group} batch {batch_number}")
    summaries: dict[str, dict[str, Any]] = {}
    if evidence_by_wallet:
        ordered_evidence = {
            candidate["wallet"]: evidence_by_wallet[candidate["wallet"]]
            for candidate in candidates
            if candidate["wallet"] in evidence_by_wallet
        }
        comparison = run_profitability_analysis(
            list(ordered_evidence), ordered_evidence, group_root / "results",
        )
        summaries = comparison["wallets"]
    results = list(failures)
    for candidate in candidates:
        wallet = candidate["wallet"]
        if wallet in summaries:
            results.append(result_from_summary(
                candidate, summaries[wallet], audits_by_wallet[wallet],
                batch_number=batch_number or 0,
            ))
    results.sort(key=lambda row: [candidate["wallet"] for candidate in candidates].index(row["wallet"]))
    return results, requests


def _run_saved_control_group(
    manifest_path: Path,
    *,
    group_root: Path,
    date_from: date,
    date_to: date,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Replay the previously accepted v1.1 control evidence without network."""
    wallets = list(CONTROL_WALLETS.values())
    evidence_by_wallet = load_profitability_evidence(
        manifest_path.resolve(), wallets, date_from, date_to,
    )
    ordered_evidence = {wallet: evidence_by_wallet[wallet] for wallet in wallets}
    comparison = run_profitability_analysis(
        wallets, ordered_evidence, group_root / "results",
    )
    candidates = [
        {"discovery_priority_rank": None, "wallet": wallet, "selection_channel": f"KNOWN_PROFITABILITY_CONTROL_{label}"}
        for label, wallet in CONTROL_WALLETS.items()
    ]
    return [
        result_from_summary(
            candidate,
            comparison["wallets"][candidate["wallet"]],
            evidence_by_wallet[candidate["wallet"]].get("profitability_event_audit") or [],
            batch_number=0,
        )
        for candidate in candidates
    ], []


def _counts(results: Iterable[dict[str, Any]]) -> dict[str, int]:
    rows = list(results)
    return {
        "positive_pnl_wallet_count": sum(row.get("result_class") == "POSITIVE_PNL" for row in rows),
        "negative_pnl_wallet_count": sum(row.get("result_class") == "NEGATIVE_PNL" for row in rows),
        "zero_pnl_wallet_count": sum(row.get("result_class") == "ZERO_PNL" for row in rows),
        "blocked_wallet_count": sum(
            row.get("result_class") == "BLOCKED_INCOMPLETE_EVIDENCE"
            for row in rows
        ),
        "no_settled_sample_count": sum(row.get("result_class") == "NO_SETTLED_SAMPLE" for row in rows),
    }


def run_phase2_batch(
    *,
    candidate_pool_path: Path,
    target_markets_path: Path,
    output_root: Path,
    discovery_commit: str,
    profitability_code_commit: str,
    batch_size: int = BATCH_SIZE,
    saved_control_profitability_manifest: Path | None = None,
) -> dict[str, Any]:
    """Run controls first, then the frozen candidates in checkpointed batches."""
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    candidate_info = load_frozen_candidate_pool(candidate_pool_path)
    target_markets = json.loads(target_markets_path.resolve().read_text(encoding="utf-8"))
    if not isinstance(target_markets, list) or not target_markets:
        raise BatchProfitabilityError("target market evidence is missing")
    target_events = {
        (str(row.get("event_id") or ""), str(row.get("event_slug") or ""))
        for row in target_markets
    }
    target_conditions = {str(row.get("condition_id") or "").lower() for row in target_markets}
    if len(target_events) != 144 or len(target_conditions) != 1584:
        raise BatchProfitabilityError(
            f"target market universe mismatch: events={len(target_events)} conditions={len(target_conditions)}"
        )
    if any(
        str(row.get("canonical_city") or "") != CITY
        or not DATE_FROM <= str(row.get("weather_date_local") or "") <= DATE_TO
        for row in target_markets
    ):
        raise BatchProfitabilityError("target market evidence is outside the fixed Beijing date range")
    output_root = output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    date_from = date.fromisoformat(DATE_FROM)
    date_to = date.fromisoformat(DATE_TO)

    control_candidates = [
        {"discovery_priority_rank": None, "wallet": wallet, "selection_channel": f"KNOWN_PROFITABILITY_CONTROL_{label}"}
        for label, wallet in CONTROL_WALLETS.items()
    ]
    if saved_control_profitability_manifest is not None:
        control_results, control_requests = _run_saved_control_group(
            saved_control_profitability_manifest,
            group_root=output_root / "controls",
            date_from=date_from,
            date_to=date_to,
        )
    else:
        control_results, control_requests = _run_wallet_group(
            control_candidates, target_markets=target_markets, date_from=date_from,
            date_to=date_to, group_root=output_root / "controls", group="controls",
            batch_number=None,
        )
    controls_by_label = {label: next(row for row in control_results if row["wallet"] == wallet) for label, wallet in CONTROL_WALLETS.items()}
    try:
        control_match_status = require_controls_match(controls_by_label)
    except ControlGroupFailure:
        manifest = {
            "schema_version": "polymarket_highest_temperature_trader_profitability_batch_v1",
            "discovery_commit": discovery_commit,
            "profitability_code_commit": profitability_code_commit,
            "city": CITY, "date_from": DATE_FROM, "date_to": DATE_TO,
            "candidate_pool_source": str(candidate_info["path"]),
            "candidate_pool_sha256": candidate_info["candidate_pool_sha256"],
            "candidate_wallet_set_sha256": candidate_info["candidate_wallet_set_sha256"],
            "candidate_wallet_count": 30, "control_wallet_count": 2,
            "batch_size": batch_size, "batch_count": 6, "batch_completed_count": 0,
            "pnl_source": PNL_SOURCE,
            "control_evidence_manifest": str(saved_control_profitability_manifest.resolve()) if saved_control_profitability_manifest else None,
            "control_a": controls_by_label["A"], "control_b": controls_by_label["B"],
            "control_a_pnl_match": "NO" if not control_match(controls_by_label["A"], CONTROL_EXPECTED_PNL["A"]) else "YES",
            "control_b_pnl_match": "NO" if not control_match(controls_by_label["B"], CONTROL_EXPECTED_PNL["B"]) else "YES",
            "batch_profitability_status": "NEEDS_REVIEW",
            "request_failure_count": sum(row.get("success") is not True for row in control_requests),
            "retry_count": sum(int(row.get("retries") or 0) for row in control_requests),
        }
        write_json(output_root / "batch_manifest.json", manifest)
        raise

    checkpoint_path = output_root / "checkpoints" / "latest.json"
    candidate_requests: list[dict[str, Any]] = []

    def run_candidate_batch(batch: list[dict[str, Any]], number: int) -> list[dict[str, Any]]:
        results, requests = _run_wallet_group(
            batch, target_markets=target_markets, date_from=date_from,
            date_to=date_to, group_root=output_root / "candidates" / f"batch_{number:02d}",
            group=f"candidate_batch_{number:02d}", batch_number=number,
        )
        candidate_requests.extend(requests)
        return results

    candidate_results = run_checkpointed_batches(
        candidate_info["candidates"], batch_size=batch_size,
        checkpoint_path=checkpoint_path, candidate_info=candidate_info,
        runner=run_candidate_batch,
    )
    candidate_results = rank_settled_results(candidate_results)
    requests = control_requests + candidate_requests
    failures = [row for row in candidate_results if row.get("result_class") in {"BLOCKED_INCOMPLETE_EVIDENCE", "NO_SETTLED_SAMPLE"}]
    write_csv(output_root / "candidate_profitability.csv", candidate_results, RESULT_FIELDS)
    write_json(output_root / "candidate_profitability_failures.json", failures)
    write_json(output_root / "batch_request_audit.json", requests)
    ranked = sorted(
        (row for row in candidate_results if row.get("settled_pnl_rank") is not None),
        key=lambda row: int(row["settled_pnl_rank"]),
    )
    counts = _counts(candidate_results)
    summary = {
        "schema_version": "polymarket_highest_temperature_trader_profitability_batch_v1",
        "pnl_source": PNL_SOURCE,
        "control_evidence_manifest": str(saved_control_profitability_manifest.resolve()) if saved_control_profitability_manifest else None,
        "city": CITY, "date_from": DATE_FROM, "date_to": DATE_TO,
        "candidate_wallet_count": len(candidate_results),
        "candidate_wallet_set_sha256": candidate_info["candidate_wallet_set_sha256"],
        "input_candidate_wallet_set_unchanged": (
            "YES" if {row["wallet"] for row in candidate_results} == set(candidate_info["wallets"]) else "NO"
        ),
        "control_a": controls_by_label["A"], "control_b": controls_by_label["B"],
        "control_a_pnl_match": "YES", "control_b_pnl_match": "YES",
        "batch_size": batch_size,
        "batch_count": (len(candidate_info["candidates"]) + batch_size - 1) // batch_size,
        "batch_completed_count": len({int(row["batch_number"]) for row in candidate_results}),
        "successful_wallet_count": sum(row.get("profitability_status") in {"READY", "PARTIAL"} for row in candidate_results),
        "acceptable_partial_wallet_count": sum(row.get("evidence_status") == "PARTIAL_HISTORICAL_UNRESOLVED" for row in candidate_results),
        **counts,
        "top_settled_pnl_results": ranked[:10],
        "known_unresolved_handling_status": "PASS",
        "sanity_check_status": "PASS" if (
            len(candidate_results) == 30
            and len({row["wallet"] for row in candidate_results}) == 30
            and not set(candidate_info["wallets"]).intersection(CONTROL_WALLETS.values())
            and all(WALLET_RE.fullmatch(row["wallet"]) for row in candidate_results)
            and all(row.get("result_class") != "BLOCKED_INCOMPLETE_EVIDENCE" for row in candidate_results)
        ) else "NEEDS_REVIEW",
        "request_failure_count": sum(row.get("request_status") == "FAILED" for row in candidate_results),
        "retry_count": sum(int(row.get("retries") or 0) for row in requests),
        "all_results": candidate_results,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    write_json(output_root / "batch_profitability_summary.json", summary)
    (output_root / "batch_profitability_summary.md").write_text(
        "# Phase 2 Beijing Hybrid Profitability\n\n"
        f"PNL_SOURCE={PNL_SOURCE}\n\n"
        f"Candidate wallets: {len(candidate_results)}\n\n"
        + "\n".join(
            f"{row.get('settled_pnl_rank') or '—'} | {row['wallet']} | {row.get('total_settled_pnl_usd') or '—'} | {row['profitability_status']}"
            for row in ranked[:10]
        ) + "\n",
        encoding="utf-8",
    )
    batch_manifest = {
        "schema_version": "polymarket_highest_temperature_trader_profitability_batch_v1",
        "discovery_commit": discovery_commit,
        "profitability_code_commit": profitability_code_commit,
        "city": CITY, "date_from": DATE_FROM, "date_to": DATE_TO,
        "candidate_pool_source": str(candidate_info["path"]),
        "candidate_pool_sha256": candidate_info["candidate_pool_sha256"],
        "candidate_wallet_set_sha256": candidate_info["candidate_wallet_set_sha256"],
        "candidate_wallet_count": 30, "control_wallet_count": 2,
        "batch_size": batch_size,
        "batch_count": summary["batch_count"],
        "successful_wallet_count": summary["successful_wallet_count"],
        "acceptable_partial_wallet_count": summary["acceptable_partial_wallet_count"],
        "blocked_wallet_count": summary["blocked_wallet_count"],
        "positive_pnl_wallet_count": summary["positive_pnl_wallet_count"],
        "negative_pnl_wallet_count": summary["negative_pnl_wallet_count"],
        "zero_pnl_wallet_count": summary["zero_pnl_wallet_count"],
        "generated_at_utc": summary["generated_at_utc"],
        "pnl_source": PNL_SOURCE,
        "control_a_pnl_match": "YES", "control_b_pnl_match": "YES",
        "batch_profitability_status": "READY_FOR_REVIEW" if summary["sanity_check_status"] == "PASS" else "NEEDS_REVIEW",
    }
    write_json(output_root / "batch_manifest.json", batch_manifest)
    return {"summary": summary, "manifest": batch_manifest, "results": candidate_results}
