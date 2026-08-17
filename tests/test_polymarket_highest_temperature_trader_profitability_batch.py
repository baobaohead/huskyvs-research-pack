from __future__ import annotations

import csv
from pathlib import Path

import pytest

from src.polymarket_highest_temperature_trader_profitability_batch import (
    CONTROL_WALLETS,
    ControlGroupFailure,
    _counts,
    _checkpoint_payload,
    failure_result,
    load_frozen_candidate_pool,
    rank_settled_results,
    require_controls_match,
    result_from_summary,
    run_checkpointed_batches,
)


def wallet(index: int) -> str:
    return "0x" + f"{index:040x}"


def write_pool(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["discovery_priority_rank", "wallet", "selection_channel"])
        writer.writeheader()
        writer.writerows(rows)


def pool_rows() -> list[dict[str, object]]:
    return [
        {"discovery_priority_rank": index, "wallet": wallet(index), "selection_channel": "LONG_TERM_ACTIVE"}
        for index in range(1, 31)
    ]


def test_candidate_pool_read_freezes_wallet_set_and_rank() -> None:
    rows = pool_rows()
    path = Path("/tmp/phase2-candidate-pool.csv")
    try:
        write_pool(path, rows)
        frozen = load_frozen_candidate_pool(path)
    finally:
        path.unlink(missing_ok=True)
    assert frozen["candidate_wallet_count"] == 30
    assert len(frozen["wallets"]) == len(set(frozen["wallets"])) == 30
    assert frozen["candidates"][0]["discovery_priority_rank"] == 1
    assert len(frozen["candidate_wallet_set_sha256"]) == 64


def test_candidate_pool_rejects_duplicate_and_a_b_wallets() -> None:
    rows = pool_rows()
    rows[-1]["wallet"] = rows[0]["wallet"]
    path = Path("/tmp/phase2-candidate-pool-duplicate.csv")
    try:
        write_pool(path, rows)
        with pytest.raises(Exception, match="duplicate"):
            load_frozen_candidate_pool(path)
    finally:
        path.unlink(missing_ok=True)

    rows = pool_rows()
    rows[-1]["wallet"] = CONTROL_WALLETS["A"]
    path = Path("/tmp/phase2-candidate-pool-control.csv")
    try:
        write_pool(path, rows)
        with pytest.raises(Exception, match="benchmark"):
            load_frozen_candidate_pool(path)
    finally:
        path.unlink(missing_ok=True)


def test_control_failure_stops_before_candidate_batches() -> None:
    with pytest.raises(ControlGroupFailure):
        require_controls_match({
            "A": {"evidence_complete_for_resolved_subset": "YES", "total_settled_pnl_usd": 0},
            "B": {"evidence_complete_for_resolved_subset": "YES", "total_settled_pnl_usd": 3180.8628},
        })


def test_wallet_failure_is_blocked_and_never_zero_pnl() -> None:
    row = failure_result({"wallet": wallet(1), "discovery_priority_rank": 1, "selection_channel": "LONG_TERM_ACTIVE"}, "REQUEST_FAILED", batch_number=1)
    assert row["result_class"] == "BLOCKED_INCOMPLETE_EVIDENCE"
    assert row["total_settled_pnl_usd"] is None
    assert _counts([row])["blocked_wallet_count"] == 1


def test_known_unresolved_partial_is_acceptable_for_resolved_subset() -> None:
    candidate = {"wallet": wallet(1), "discovery_priority_rank": 1, "selection_channel": "LONG_TERM_ACTIVE"}
    summary = {
        "PROFITABILITY_STATUS": "PARTIAL",
        "PROFITABILITY_STATUS_REASONS": [],
        "TOTAL_SETTLED_PNL_USD": 12.5,
        "SETTLED_MARKET_WEATHER_DAYS": 3,
        "PROFITABLE_DAYS": 2,
        "LOSS_DAYS": 1,
        "MONTHS_WITH_POSITIVE_PNL": 1,
        "MONTHS_WITH_NEGATIVE_PNL": 0,
        "MONTHLY_PNL": {"2026-03": 12.5},
        "PROFITABILITY_STABILITY": "INSUFFICIENT_DATA",
    }
    result = result_from_summary(
        candidate, summary,
        [
            {"settlement_status": "RESOLVED", "request_status": "COMPLETE"},
            {"settlement_status": "NOT_RESOLVED", "request_status": "EXCLUDED_NOT_RESOLVED"},
        ],
        batch_number=1,
    )
    assert result["evidence_complete_for_resolved_subset"] == "YES"
    assert result["evidence_status"] == "PARTIAL_HISTORICAL_UNRESOLVED"
    assert result["result_class"] == "POSITIVE_PNL"
    assert "KNOWN_HISTORICAL_UNRESOLVED_MARKET" in result["status_reason"]


def test_blocked_evidence_remains_blocked() -> None:
    candidate = {"wallet": wallet(1), "discovery_priority_rank": 1, "selection_channel": "LONG_TERM_ACTIVE"}
    result = result_from_summary(
        candidate,
        {"PROFITABILITY_STATUS": "BLOCKED", "PROFITABILITY_STATUS_REASONS": ["REQUEST_FAILED"]},
        [{"settlement_status": "RESOLVED", "request_status": "REQUEST_FAILED"}],
        batch_number=1,
    )
    assert result["result_class"] == "BLOCKED_INCOMPLETE_EVIDENCE"
    assert result["total_settled_pnl_usd"] is None


def test_checkpoint_resume_skips_completed_batch_and_ranking_is_deterministic(tmp_path: Path) -> None:
    candidates = [
        {"discovery_priority_rank": index, "wallet": wallet(index), "selection_channel": "LONG_TERM_ACTIVE"}
        for index in range(1, 11)
    ]
    candidate_info = {
        "candidate_wallet_set_sha256": "a" * 64,
        "wallets": [row["wallet"] for row in candidates],
    }
    first_results = [
        {"wallet": row["wallet"], "total_settled_pnl_usd": float(row["discovery_priority_rank"]), "settled_market_weather_days": 1, "evidence_complete_for_resolved_subset": "YES"}
        for row in candidates[:5]
    ]
    checkpoint = tmp_path / "latest.json"
    from src.polymarket_highest_temperature_trader_profitability_batch import write_json
    write_json(checkpoint, _checkpoint_payload(candidate_info, [1], first_results))
    calls: list[int] = []

    def runner(batch: list[dict[str, object]], number: int) -> list[dict[str, object]]:
        calls.append(number)
        return [
            {"wallet": row["wallet"], "total_settled_pnl_usd": float(row["discovery_priority_rank"]), "settled_market_weather_days": 1, "evidence_complete_for_resolved_subset": "YES"}
            for row in batch
        ]

    results = run_checkpointed_batches(
        candidates, batch_size=5, checkpoint_path=checkpoint,
        candidate_info=candidate_info, runner=runner,
    )
    assert calls == [2]
    assert len(results) == 10
    ranked = rank_settled_results([
        {"wallet": wallet(2), "total_settled_pnl_usd": 5, "settled_market_weather_days": 1, "evidence_complete_for_resolved_subset": "YES"},
        {"wallet": wallet(1), "total_settled_pnl_usd": 5, "settled_market_weather_days": 1, "evidence_complete_for_resolved_subset": "YES"},
    ])
    by_wallet = {row["wallet"]: row for row in ranked}
    assert by_wallet[wallet(1)]["settled_pnl_rank"] == 1
    assert by_wallet[wallet(2)]["settled_pnl_rank"] == 2
