from __future__ import annotations

import csv
import json
import shutil
import socket
import tempfile
from pathlib import Path

import pytest

from src.husky_beijing_price_outcome_study_v1 import (
    ANALYSIS_CUTOFF_UTC,
    HUSKY_WALLET,
    ACCOUNT_CONNECTION,
    FORMAL_STARTED,
    PUBLIC_DATA_ONLY,
    PUBLIC_GET_ONLY,
    REAL_ORDER,
    SIGNING,
    analyze,
    add_behavior_rows,
    annotate_outcome_adds,
    basically_no_buy_above,
    bucket_details,
    buckets_adjacent,
    classify_event_buys,
    closest_scenario_examples,
    event_yes_summary,
    example_rows,
    meaningful_max_price,
    mixed_event_row,
    multi_yes_event_summary,
    nearest_rank,
    no_implied_threshold_summary,
    normalize_trade_rows,
    price_band,
    price_band_summary,
    strict_pnl_by_yes_price_band,
    weighted_quantile,
    yes_threshold_summary,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
TEMP_ROOT = Path("/tmp/husky_beijing_price_outcome_study_v1")


def fill(
    *,
    event: str = "2026-07-20__beijing__high",
    outcome: str = "YES",
    side: str = "BUY",
    bucket: str = "30°C",
    asset: str = "yes-30",
    price: float = 0.20,
    shares: float = 10,
    usd: float | None = None,
    timestamp: int = 1,
    bucket_kind: str = "exact",
    bucket_low: float = 30,
) -> dict:
    return {
        "event_key": event,
        "weather_date": event[:10],
        "condition_id": f"condition-{event}",
        "asset": asset,
        "temperature_bucket": bucket,
        "outcome": outcome,
        "side": side,
        "price": price,
        "shares": shares,
        "trade_usd": price * shares if usd is None else usd,
        "transaction_hash": f"tx-{event}-{asset}-{side}-{timestamp}-{price}",
        "timestamp_epoch": timestamp,
        "public_trade_time_cst": f"{event[:10]}T10:00:00+08:00",
        "bucket_kind": bucket_kind,
        "bucket_low": bucket_low,
        "bucket_high": bucket_low,
        "unit": "C",
    }


@pytest.fixture(scope="module")
def real_analysis() -> tuple[dict, Path]:
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="pytest.", dir=TEMP_ROOT))
    summary = analyze(
        REPO_ROOT,
        root / "output",
        root / "summary.md",
        root / "summary.json",
    )
    yield summary, root
    shutil.rmtree(root)


def test_buy_yes_and_buy_no_are_strictly_separate():
    rows = [
        fill(outcome="YES", asset="yes"),
        fill(outcome="NO", asset="no"),
    ]
    result = classify_event_buys(rows)
    assert result["event_buy_structure"] == "MIXED_YES_AND_NO"
    assert result["yes_bucket_count"] == 1
    assert result["no_bucket_count"] == 1


def test_outcome_and_side_case_are_normalized():
    row = normalize_trade_rows([
        fill(outcome=" yes ", side=" buy ")
    ])[0]
    assert row["outcome"] == "YES"
    assert row["side"] == "BUY"


def test_sell_does_not_enter_buy_structure():
    rows = [fill(outcome="YES", side="SELL")]
    assert classify_event_buys(rows)["event_buy_structure"] == "NO_BUY"


def test_same_temperature_yes_and_no_are_not_merged():
    result = classify_event_buys([
        fill(outcome="YES", asset="yes-30"),
        fill(outcome="NO", asset="no-30"),
    ])
    assert result["yes_bucket_count"] == result["no_bucket_count"] == 1
    assert result["same_bucket_both_sides"]


def test_buy_no_implied_yes_is_one_minus_price(real_analysis):
    _, root = real_analysis
    rows = list(csv.DictReader((root / "output/no_buy_fills.csv").open()))
    assert rows
    for row in rows:
        assert float(row["implied_yes_equivalent_price"]) == pytest.approx(
            1 - float(row["no_price"])
        )


def test_yes_only_event():
    result = classify_event_buys([fill(outcome="YES")])
    assert result["event_buy_structure"] == "YES_ONLY"


def test_no_only_event():
    result = classify_event_buys([fill(outcome="NO")])
    assert result["event_buy_structure"] == "NO_ONLY"


def test_mixed_yes_and_no_event():
    result = classify_event_buys([
        fill(outcome="YES", bucket="30°C", asset="yes-30"),
        fill(outcome="NO", bucket="29°C", asset="no-29"),
    ])
    assert result["event_buy_structure"] == "MIXED_YES_AND_NO"


def test_same_bucket_both_sides_subtype():
    result = classify_event_buys([
        fill(outcome="YES", bucket="30°C", asset="yes-30"),
        fill(outcome="NO", bucket="30°C", asset="no-30"),
    ])
    assert result["mixed_yes_no_subtype"] == "SAME_BUCKET_BOTH_SIDES"


def test_cross_bucket_yes_no_subtype():
    result = classify_event_buys([
        fill(outcome="YES", bucket="30°C", asset="yes-30"),
        fill(outcome="NO", bucket="29°C", asset="no-29"),
    ])
    assert result["mixed_yes_no_subtype"] == "CROSS_BUCKET_YES_NO"


def test_both_mixed_subtype():
    result = classify_event_buys([
        fill(outcome="YES", bucket="30°C", asset="yes-30"),
        fill(outcome="YES", bucket="31°C", asset="yes-31"),
        fill(outcome="NO", bucket="30°C", asset="no-30"),
    ])
    assert result["mixed_yes_no_subtype"] == "BOTH"


def test_legacy_bucket_counts_reproduce_40_and_31(real_analysis):
    summary, _ = real_analysis
    assert summary["legacy_bucket_statistics"]["multi_bucket_event_count"] == 40
    assert summary["legacy_bucket_statistics"]["adjacent_bucket_event_count"] == 31


def test_yes_only_multi_bucket_count(real_analysis):
    summary, _ = real_analysis
    assert summary["corrected_bucket_statistics"]["yes_multi_bucket_event_count"] == 29


def test_yes_only_adjacent_count(real_analysis):
    summary, _ = real_analysis
    assert (
        summary["corrected_bucket_statistics"]["yes_adjacent_basket_event_count"]
        == 21
    )


def test_no_exclusion_statistics(real_analysis):
    summary, _ = real_analysis
    corrected = summary["corrected_bucket_statistics"]
    assert corrected["no_multi_bucket_event_count"] == 1
    assert corrected["no_adjacent_exclusion_event_count"] == 1


def test_no_never_enters_yes_multi_bucket():
    rows = [
        fill(outcome="YES", bucket="30°C", asset="yes-30"),
        fill(outcome="NO", bucket="31°C", asset="no-31"),
    ]
    assert multi_yes_event_summary(rows[0]["event_key"], rows) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.0, "0—1美分"),
        (0.009999, "0—1美分"),
        (0.01, "1—2美分"),
        (0.02, "2—5美分"),
        (0.05, "5—10美分"),
        (0.10, "10—15美分"),
        (0.15, "15—20美分"),
        (0.20, "20—30美分"),
        (0.30, "30—40美分"),
        (0.40, "40—50美分"),
        (0.50, "50—60美分"),
        (0.55, "50—60美分"),
        (0.60, "60—70美分"),
        (0.70, "70—80美分"),
        (0.80, "80—90美分"),
        (0.90, "90—100美分"),
        (1.0, "90—100美分"),
    ],
)
def test_price_band_boundaries(value, expected):
    assert price_band(value) == expected


def test_usd_weighted_price_quantile():
    rows = [
        fill(price=0.01, shares=100, usd=1),
        fill(price=0.90, shares=10, usd=9, timestamp=2),
    ]
    assert weighted_quantile(rows, 0.50) == 0.90


def test_fill_price_quantile_uses_nearest_rank():
    assert nearest_rank([0.01, 0.02, 0.03, 0.04], 0.75) == 0.03


def test_event_max_price_quantile(real_analysis):
    summary, _ = real_analysis
    metric = summary["yes_price_quantiles"]["event_level"][
        "maximum_yes_buy_price"
    ]
    assert metric["p95"] == pytest.approx(0.3300000059)
    assert metric["max"] == pytest.approx(0.46)


def test_meaningful_max_rule():
    rows = [
        fill(event=f"2026-07-0{i}__beijing__high", price=0.30, usd=2, asset=f"a{i}")
        for i in range(1, 4)
    ]
    rows.append(fill(price=0.80, usd=100, asset="one-event", timestamp=2))
    assert meaningful_max_price(rows) == 0.30


def test_meaningful_max_not_established():
    assert meaningful_max_price([fill(price=0.50, usd=4)]) == "NOT_ESTABLISHED"


def test_basically_no_buy_rule():
    rows = [
        fill(event="2026-07-01__beijing__high", price=0.10, usd=100, asset="a"),
        fill(event="2026-07-02__beijing__high", price=0.50, usd=0.5, asset="b"),
    ]
    assert basically_no_buy_above(rows) == 0.15


def test_no_clear_ceiling_rule():
    rows = [
        fill(
            event=f"2026-07-{i:02d}__beijing__high",
            price=0.90,
            usd=10,
            asset=f"a{i}",
        )
        for i in range(1, 4)
    ]
    assert basically_no_buy_above(rows) == "NO_CLEAR_CEILING"


def test_high_price_examples_are_complete(real_analysis):
    summary, root = real_analysis
    rows = list(csv.DictReader(
        (root / "output/high_price_yes_examples.csv").open()
    ))
    assert rows
    required = {
        "weather_date", "timestamp_cst", "temperature_bucket", "outcome",
        "price_decimal", "shares", "trade_usd", "event_total_yes_buy_usd",
        "fill_share_of_event_yes_buy_usd", "bucket_total_yes_buy_usd",
        "bucket_is_event_dominant_yes", "same_event_bought_other_yes_bucket",
        "same_event_bought_no", "entry_path_completeness",
        "strict_pnl_available", "strict_pnl",
    }
    assert required <= set(rows[0])
    assert max(float(row["price_decimal"]) for row in rows) == pytest.approx(
        summary["price_ceiling"]["absolute_max_yes_buy_price"]
    )


def test_low_price_shares_and_usd_are_separate():
    rows = [
        fill(price=0.01, shares=1000, usd=10),
        fill(price=0.50, shares=100, usd=50, timestamp=2),
    ]
    summary = price_band_summary(rows, "YES")
    low = next(row for row in summary if row["price_band"] == "1—2美分")
    assert low["buy_shares"] == 1000
    assert low["buy_usd"] == 10
    assert low["buy_usd_share"] == pytest.approx(1 / 6)


def test_multi_yes_event_expensive_and_cheap():
    rows = [
        fill(bucket="30°C", asset="cheap", price=0.10, shares=10, timestamp=1),
        fill(bucket="31°C", asset="expensive", price=0.40, shares=10, timestamp=2),
    ]
    result = multi_yes_event_summary(rows[0]["event_key"], rows)
    assert result["cheapest_yes_bucket"] == "30°C"
    assert result["most_expensive_yes_bucket"] == "31°C"


def test_dominant_yes_bucket_uses_usd_not_shares():
    rows = [
        fill(bucket="30°C", asset="many", price=0.01, shares=100, usd=1),
        fill(bucket="31°C", asset="few", price=0.50, shares=10, usd=5, timestamp=2),
    ]
    result = multi_yes_event_summary(rows[0]["event_key"], rows)
    assert result["dominant_yes_bucket_by_usd"] == "31°C"


def test_scenario_selector_finds_exact_style():
    rows = [
        fill(bucket="35°C", asset="h", price=0.55, shares=10, timestamp=1, bucket_low=35),
        fill(bucket="36°C", asset="a", price=0.20, shares=10, timestamp=2, bucket_low=36),
        fill(bucket="37°C", asset="f", price=0.01, shares=10, timestamp=3, bucket_low=37),
    ]
    multi = multi_yes_event_summary(rows[0]["event_key"], rows)
    examples, exact = closest_scenario_examples([multi])
    assert exact == 1
    assert examples[0]["exact_style_match"]


def test_scenario_selector_does_not_fabricate_when_no_candidate():
    rows = [
        fill(bucket="35°C", asset="h", price=0.55, timestamp=1, bucket_low=35),
        fill(bucket="36°C", asset="a", price=0.20, timestamp=2, bucket_low=36),
    ]
    multi = multi_yes_event_summary(rows[0]["event_key"], rows)
    examples, exact = closest_scenario_examples([multi])
    assert exact == 0
    assert examples == []


def test_scenario_selector_uses_relaxed_adjacent_rule():
    rows = [
        fill(
            bucket="35°C", asset="h", price=0.55, timestamp=1,
            bucket_low=35,
        ),
        fill(
            bucket="36°C", asset="l", price=0.05, timestamp=2,
            bucket_low=36,
        ),
    ]
    multi = multi_yes_event_summary(rows[0]["event_key"], rows)
    examples, exact = closest_scenario_examples([multi])
    assert exact == 0
    assert examples[0]["relaxed_style_match"]


def test_yes_and_no_adds_are_separate():
    rows = [
        fill(outcome="YES", asset="yes", price=0.10, timestamp=1),
        fill(outcome="NO", asset="no", price=0.90, timestamp=2),
        fill(outcome="YES", asset="yes", price=0.20, timestamp=3),
        fill(outcome="NO", asset="no", price=0.80, timestamp=4),
    ]
    annotated = annotate_outcome_adds(rows)
    yes_add = next(
        row for row in annotated
        if row["outcome"] == "YES" and row["timestamp_epoch"] == 3
    )
    no_add = next(
        row for row in annotated
        if row["outcome"] == "NO" and row["timestamp_epoch"] == 4
    )
    assert yes_add["outcome_price_add_class"] == "PRICE_UP_ADD"
    assert no_add["outcome_price_add_class"] == "PRICE_DOWN_ADD"


def test_add_behavior_rows_use_six_requested_price_bands():
    rows = annotate_outcome_adds([
        fill(price=0.01, timestamp=1),
        fill(price=0.02, timestamp=2),
    ])
    result = add_behavior_rows(rows, "YES")
    assert [row["price_band"] for row in result] == [
        "<5美分", "5—10美分", "10—20美分",
        "20—30美分", "30—50美分", ">=50美分",
    ]


def test_mixed_event_summary_records_order_and_ratios():
    rows = [
        fill(outcome="NO", asset="n", timestamp=1, usd=2),
        fill(outcome="YES", asset="y", timestamp=2, usd=1),
    ]
    classification = classify_event_buys(rows)
    result = mixed_event_row(rows[0]["event_key"], rows, classification)
    assert result["first_outcome_order"] == "NO_THEN_YES"
    assert result["no_buy_usd_share"] == pytest.approx(2 / 3)
    assert result["buy_sequence"][0]["implied_yes_equivalent_price"] == 0.8


def test_strict_pnl_excludes_unaligned_assets():
    yes = [fill(asset="a", price=0.20, shares=10, usd=2)]
    closed = [{
        "asset": "a", "outcome": "Yes", "totalBought": 11,
        "avgPrice": 0.20, "realizedPnl": 5,
    }]
    _, status = strict_pnl_by_yes_price_band(yes, closed)
    assert status["strict_aligned_yes_asset_count"] == 0
    assert status["excluded_yes_asset_count"] == 1


def test_strict_pnl_never_uses_event_attribution(real_analysis):
    summary, _ = real_analysis
    status = summary["strict_pnl_price_band"]
    assert not status["event_pnl_attribution_used"]
    assert not status["unvalidated_resolved_snapshot_included"]


def test_real_core_counts_remain_fixed(real_analysis):
    summary, _ = real_analysis
    assert summary["beijing_event_count"] == 50
    assert summary["total_public_fill_count"] == 537
    assert summary["public_buy_fill_count"] == 453
    assert summary["public_sell_fill_count"] == 84


def test_real_yes_no_counts_are_exhaustive(real_analysis):
    summary, _ = real_analysis
    assert summary["buy_yes_fill_count"] == 400
    assert summary["buy_no_fill_count"] == 53
    assert (
        summary["buy_yes_fill_count"] + summary["buy_no_fill_count"]
        == summary["public_buy_fill_count"]
    )


def test_portable_sha_verification_passes(real_analysis):
    summary, _ = real_analysis
    assert summary["source_evidence"]["portable_sha_verification"] == "PASS"


def test_full_market_favorite_is_not_supported(real_analysis):
    summary, _ = real_analysis
    assert (
        summary["full_market_favorite_at_buy_time_status"]
        == "NOT_SUPPORTED_BY_CURRENT_EVIDENCE"
    )


def test_offline_analysis_makes_zero_network_calls(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", denied)
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="offline.", dir=TEMP_ROOT))
    try:
        summary = analyze(
            REPO_ROOT,
            root / "output",
            root / "summary.md",
            root / "summary.json",
        )
        assert summary["offline_network_call_count"] == 0
    finally:
        shutil.rmtree(root)


def test_source_manifest_uses_relative_paths(real_analysis):
    _, root = real_analysis
    manifest = json.loads(
        (root / "output/source_manifest.json").read_text()
    )
    assert manifest["raw_evidence_copied_to_output"] is False
    assert not Path(manifest["relative_path"]).is_absolute()
    assert ".." not in Path(manifest["relative_path"]).parts
    assert "manifest_sha256" in manifest
    assert "sources" not in manifest
    assert manifest["offline_only"] is True
    assert manifest["source_record_counts"] == {
        "activity": 537,
        "closed_positions": 54,
        "positions": 71,
        "profile": 1,
        "trades": 537,
    }


def test_required_output_files_exist(real_analysis):
    _, root = real_analysis
    required = {
        "yes_buy_fills.csv",
        "no_buy_fills.csv",
        "yes_price_band_summary.csv",
        "no_price_band_summary.csv",
        "yes_price_threshold_summary.csv",
        "yes_event_price_summary.csv",
        "yes_multi_bucket_event_summary.csv",
        "no_event_summary.csv",
        "mixed_yes_no_event_summary.csv",
        "high_price_yes_examples.csv",
        "low_price_yes_examples.csv",
        "scenario_35_36_37_style_examples.csv",
        "legacy_vs_corrected_bucket_findings.csv",
        "strict_pnl_by_yes_price_band.csv",
        "yes_add_behavior_summary.csv",
        "no_add_behavior_summary.csv",
        "source_manifest.json",
    }
    assert required == {
        path.name for path in (root / "output").iterdir()
    }


def test_safety_flags():
    assert PUBLIC_DATA_ONLY
    assert PUBLIC_GET_ONLY
    assert not ACCOUNT_CONNECTION
    assert not SIGNING
    assert not REAL_ORDER
    assert not FORMAL_STARTED
    assert HUSKY_WALLET == "0xaf17116ae2b1476032785a67bd5b7c8c05905c20"
    assert ANALYSIS_CUTOFF_UTC == "2026-07-29T03:30:01.944885+00:00"


def test_old_event_pnl_is_not_assigned_to_fill():
    row = fill(price=0.40)
    old = {
        row["event_key"]: {
            "entry_timeline_status": "ENTRY_TIMELINE_COMPLETE",
            "pnl_status": "STRICT_CLOSED_SETTLED",
            "strict_pnl": "10",
        }
    }
    examples = example_rows(
        [row], {row["event_key"]: [row]}, old, low=False
    )
    assert "not attributed to this fill" in examples[0]["strict_pnl_note"]


def test_event_summary_weighted_average_uses_trade_usd_as_weight():
    rows = [
        fill(price=0.01, shares=100, usd=1),
        fill(price=0.50, shares=10, usd=5, timestamp=2),
    ]
    event = event_yes_summary(
        rows[0]["event_key"],
        rows,
        {"weather_date": "2026-07-20"},
    )
    assert event["usd_weighted_average_yes_buy_price"] == pytest.approx(
        (0.01 * 1 + 0.50 * 5) / 6
    )


def test_bucket_adjacency_rejects_tail():
    exact = fill(bucket="35°C", bucket_low=35)
    adjacent = fill(bucket="36°C", bucket_low=36)
    tail = fill(
        bucket="35°C or below", bucket_kind="below", bucket_low=35
    )
    assert buckets_adjacent(exact, adjacent)
    assert not buckets_adjacent(tail, adjacent)


def test_threshold_summary_uses_actual_usd():
    rows = [
        fill(price=0.20, shares=1000, usd=2),
        fill(price=0.10, shares=1, usd=100, timestamp=2),
    ]
    summary = yes_threshold_summary(rows)
    at_20 = next(
        row for row in summary
        if row["threshold_decimal_inclusive"] == 0.20
    )
    assert at_20["buy_yes_usd"] == 2
    assert at_20["yes_buy_usd_share"] == pytest.approx(2 / 102)


def test_threshold_summary_includes_90_cents_and_dominant_count():
    rows = [
        fill(price=0.90, usd=10, bucket="30°C", asset="a"),
        fill(price=0.10, usd=1, bucket="31°C", asset="b", timestamp=2),
    ]
    at_90 = next(
        row for row in yes_threshold_summary(rows)
        if row["threshold_decimal_inclusive"] == 0.90
    )
    assert at_90["buy_yes_fill_count"] == 1
    assert at_90["dominant_yes_bucket_event_count"] == 1


def test_no_implied_threshold_summary_has_usd_and_events():
    rows = [
        fill(outcome="NO", price=0.01, usd=2),
        fill(
            event="2026-07-21__beijing__high",
            outcome="NO", price=0.02, usd=3,
        ),
    ]
    at_99 = next(
        row for row in no_implied_threshold_summary(rows)
        if row["implied_yes_equivalent_threshold_inclusive"] == 0.99
    )
    assert at_99["buy_no_fill_count"] == 1
    assert at_99["buy_no_usd"] == 2
    assert at_99["weather_event_count"] == 1


def test_event_summary_includes_median_and_asset_count():
    rows = [
        fill(price=0.10, asset="a"),
        fill(price=0.30, asset="b", timestamp=2),
    ]
    event = event_yes_summary(
        rows[0]["event_key"], rows, {"weather_date": "2026-07-20"}
    )
    assert event["median_yes_buy_price"] == pytest.approx(0.20)
    assert event["yes_asset_count"] == 2


def test_report_answers_all_36_questions(real_analysis):
    _, root = real_analysis
    report = (root / "summary.md").read_text()
    assert "## 36 个核心问题" in report
    for index in range(1, 37):
        assert f"{index}. **[" in report


def test_high_price_examples_include_required_audit_fields(real_analysis):
    _, root = real_analysis
    rows = list(csv.DictReader(
        (root / "output/high_price_yes_examples.csv").open()
    ))
    required = {
        "transaction_hash",
        "bucket_share_of_event_yes_buy_usd",
        "is_event_dominant_yes_bucket",
        "other_yes_buckets_in_event",
        "no_buys_present_in_event",
        "entry_timeline_status",
        "strict_pnl_status",
        "strict_event_pnl",
    }
    assert required <= set(rows[0])


def test_scenario_result_is_none_when_no_real_match(real_analysis):
    summary, root = real_analysis
    scenario = summary["scenario_35_36_37_style"]
    assert scenario["strict_style_event_count"] == 0
    assert scenario["relaxed_style_event_count"] == 0
    assert scenario["result"] == "NONE_OBSERVED"
    rows = list(csv.DictReader(
        (root / "output/scenario_35_36_37_style_examples.csv").open()
    ))
    assert rows == []
