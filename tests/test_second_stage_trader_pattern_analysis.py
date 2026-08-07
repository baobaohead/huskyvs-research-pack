from __future__ import annotations

import csv
import importlib.util
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "second_stage_trader_pattern_analysis.py"
SPEC = importlib.util.spec_from_file_location("second_stage_trader_pattern_analysis", SCRIPT_PATH)
assert SPEC and SPEC.loader
analysis = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(analysis)


BASE_LOCAL = datetime(2026, 5, 1, 0, 0, 0)


def make_row(
    offset_seconds: int,
    *,
    side: str = "BUY",
    outcome: str = "YES",
    price: float = 0.20,
    shares: float = 100.0,
    weather_date: str = "2026-05-01",
    condition_id: str = "condition-1",
    asset: str = "asset-1",
    temperature_bucket: str = "30",
    event_slug: str = "highest-temperature-in-beijing-on-may-1-2026",
    transaction_hash: str | None = None,
) -> dict[str, str]:
    local_dt = BASE_LOCAL + timedelta(seconds=offset_seconds)
    utc_dt = local_dt.replace(tzinfo=timezone.utc)
    return {
        "timestamp_epoch": str(int(utc_dt.timestamp())),
        "trade_time_market_local": local_dt.isoformat(),
        "price": str(price),
        "shares": str(shares),
        "trade_usd": str(price * shares),
        "side": side,
        "outcome": outcome,
        "weather_date_local": weather_date,
        "condition_id": condition_id,
        "asset": asset,
        "temperature_bucket": temperature_bucket,
        "event_slug": event_slug,
        "transaction_hash": transaction_hash or f"tx-{offset_seconds}-{asset}-{side}",
        "relative_weather_day": "D0",
        "report_time_bucket": "D0_00_08",
    }


def paths_from_rows(rows: list[dict[str, str]]) -> list[dict]:
    return analysis.asset_paths(analysis.normalize_fills(rows))


def one_path(rows: list[dict[str, str]]) -> dict:
    paths = paths_from_rows(rows)
    assert len(paths) == 1
    return paths[0]


def test_requested_calendar_is_inclusive_96_days() -> None:
    assert len(analysis.date_range(date(2026, 5, 1), date(2026, 8, 4))) == 96


def test_denominator_counts_96_dates_97_events_and_one_duplicate_date() -> None:
    events = [
        {"weather_date": (date(2026, 5, 1) + timedelta(days=i)).isoformat(), "event_slug": f"slug-{i}"}
        for i in range(96)
    ]
    events.append({"weather_date": "2026-05-19", "event_slug": "highest-temperature-in-beijing-on-may-19-2026"})
    result = analysis.date_denominator(Path("/unused"), date(2026, 5, 1), date(2026, 8, 4), events)
    assert result["requested_days"] == 96
    assert result["unique_weather_dates"] == 96
    assert result["event_count"] == 97
    assert result["duplicate_event_date_count"] == 1
    assert result["event_counts_by_date"]["2026-05-19"] == 2


def test_denominator_excludes_out_of_range_events_from_event_count() -> None:
    events = [
        {"weather_date": "2026-05-01", "event_slug": "in-range"},
        {"weather_date": "2026-08-05", "event_slug": "out-of-range"},
    ]
    result = analysis.date_denominator(Path("/unused"), date(2026, 5, 1), date(2026, 8, 4), events)
    assert result["event_count"] == 1
    assert result["unique_weather_dates"] == 1
    assert result["out_of_range_event_count"] == 1
    assert result["duplicate_event_date_count"] == 0


def test_denominator_detects_old_new_slug_duplicate() -> None:
    events = [
        {"weather_date": "2026-05-19", "event_slug": "arch-highest-temperature-in-beijing-on-may-19-2026"},
        {"weather_date": "2026-05-19", "event_slug": "highest-temperature-in-beijing-on-may-19-2026"},
    ]
    result = analysis.date_denominator(Path("/unused"), date(2026, 5, 1), date(2026, 8, 4), events)
    assert result["old_new_duplicate_dates"] == ["2026-05-19"]


def test_asset_path_does_not_merge_different_condition_ids() -> None:
    rows = [
        make_row(0, condition_id="condition-a", asset="asset-a"),
        make_row(1, condition_id="condition-b", asset="asset-b"),
    ]
    paths = paths_from_rows(rows)
    assert len(paths) == 2
    assert {path["condition_id"] for path in paths} == {"condition-a", "condition-b"}


def test_asset_path_does_not_merge_different_assets_or_temperature_buckets() -> None:
    rows = [
        make_row(0, asset="asset-a", temperature_bucket="30"),
        make_row(1, asset="asset-b", temperature_bucket="31"),
    ]
    paths = paths_from_rows(rows)
    assert {(path["asset"], path["temperature_bucket"]) for path in paths} == {("asset-a", "30"), ("asset-b", "31")}


def test_asset_path_keeps_outcomes_separate() -> None:
    paths = paths_from_rows([make_row(0, outcome="YES"), make_row(1, outcome="NO")])
    assert {path["outcome"] for path in paths} == {"YES", "NO"}


def test_first_any_buy_and_first_low_buy_hold_times_are_distinct() -> None:
    path = one_path([
        make_row(0, price=0.50),
        make_row(3_600, price=0.20),
        make_row(7_200, side="SELL", price=0.99, shares=20),
    ])
    result = analysis.wallet_two_high_sell_analysis([path])["low_buy_high_sell"]
    assert result["median_any_hold_seconds"] == 7_200
    assert result["median_low_hold_seconds"] == 3_600
    item = analysis.wallet_two_high_sell_analysis([path])["low_paths"][0]
    assert item["first_any_buy_to_first_high_sell_seconds"] == 7_200
    assert item["first_low_buy_to_first_high_sell_seconds"] == 3_600


def test_last_buy_to_first_sell_is_exposed_at_asset_level() -> None:
    path = one_path([
        make_row(0, price=0.20),
        make_row(3_600, price=0.10),
        make_row(7_200, side="SELL", price=0.99, shares=20),
    ])
    assert path["first_buy_to_first_sell_seconds"] == 7_200
    assert path["last_buy_to_first_sell_seconds"] == 3_600


def test_inventory_ledger_adds_buy_before_later_sell() -> None:
    path = one_path([
        make_row(0, shares=100, price=0.20),
        make_row(3_600, side="SELL", shares=90, price=0.99),
        make_row(7_200, shares=50, price=0.10),
        make_row(10_800, side="SELL", shares=60, price=0.99),
    ])
    high = path["high_sell_ledger_rows"]
    assert high[1]["cumulative_buy_shares_before"] == pytest.approx(150)
    assert high[1]["observed_net_inventory_before"] == pytest.approx(60)


def test_inventory_ledger_deducts_each_sell_sequentially() -> None:
    path = one_path([
        make_row(0, shares=100, price=0.20),
        make_row(3_600, side="SELL", shares=40, price=0.99),
        make_row(7_200, side="SELL", shares=30, price=0.99),
    ])
    high = path["high_sell_ledger_rows"]
    assert high[0]["observed_net_inventory_before"] == pytest.approx(100)
    assert high[1]["observed_net_inventory_before"] == pytest.approx(60)


def test_inventory_classifies_partial_exit() -> None:
    path = one_path([make_row(0, shares=100), make_row(3_600, side="SELL", shares=40, price=0.99)])
    row = path["high_sell_ledger_rows"][0]
    assert row["observed_exit_classification"] == "PARTIAL_OBSERVED_EXIT"
    assert row["sell_to_observed_inventory_ratio"] == pytest.approx(0.4)


def test_inventory_classifies_near_full_exit() -> None:
    path = one_path([make_row(0, shares=100), make_row(3_600, side="SELL", shares=100, price=0.99)])
    assert path["high_sell_ledger_rows"][0]["observed_exit_classification"] == "NEAR_FULL_OBSERVED_EXIT"


def test_inventory_classifies_excess_exit_after_prior_sell() -> None:
    path = one_path([
        make_row(0, shares=100),
        make_row(3_600, side="SELL", shares=90, price=0.99),
        make_row(7_200, side="SELL", shares=20, price=0.99),
    ])
    assert path["high_sell_ledger_rows"][1]["observed_exit_classification"] == "EXCEEDS_OBSERVED_INVENTORY"


def test_inventory_classifies_unknown_when_observed_inventory_is_empty() -> None:
    path = one_path([make_row(0, side="SELL", shares=10, price=0.99)])
    assert path["high_sell_ledger_rows"][0]["observed_exit_classification"] == "UNKNOWN_INVENTORY"


def test_asset_exit_classification_can_be_mixed() -> None:
    path = one_path([
        make_row(0, shares=100),
        make_row(3_600, side="SELL", shares=90, price=0.99),
        make_row(7_200, shares=90, price=0.10),
        make_row(10_800, side="SELL", shares=100, price=0.99),
    ])
    assert analysis.asset_exit_classification(path["high_sell_ledger_rows"]) == "MIXED_EXIT_PATTERN"


def test_dynamic_exit_conclusion_changes_with_observed_classes() -> None:
    partial_path = one_path([make_row(0, shares=100), make_row(3_600, side="SELL", shares=40, price=0.99)])
    near_path = one_path([
        make_row(0, asset="asset-near", shares=100),
        make_row(3_600, asset="asset-near", side="SELL", shares=100, price=0.99),
    ])
    partial_report = analysis.render_high_cases(analysis.wallet_two_high_sell_analysis([partial_path]))
    near_report = analysis.render_high_cases(analysis.wallet_two_high_sell_analysis([near_path]))
    assert "部分退出的资产更多" in partial_report
    assert "接近全部观察库存退出的资产更多" in near_report


def test_low_buy_weighted_price_uses_only_zero_to_thirty_cent_buys() -> None:
    path = one_path([
        make_row(0, price=0.60, shares=100),
        make_row(3_600, price=0.20, shares=100),
        make_row(7_200, side="SELL", price=0.99, shares=20),
    ])
    low = analysis.wallet_two_high_sell_analysis([path])["low_buy_high_sell"]
    assert low["all_prior_buy_weighted_price"] == pytest.approx(0.40)
    assert low["low_0_30_buy_weighted_price"] == pytest.approx(0.20)
    assert low["high_90_100_sell_weighted_price"] == pytest.approx(0.99)


@pytest.mark.parametrize(
    ("metrics", "expected"),
    [
        ({"buy_fill_share": 0.90, "sell_to_buy_fill_ratio": 0.10, "repeated_asset_share": 0.0, "sell_then_rebuy_ratio_decimal": 0.0, "same_hour_two_way": 0, "short_hold_ratio_decimal": 0.0}, "BUY_DOMINANT_ACCUMULATOR"),
        ({"buy_fill_share": 0.50, "sell_to_buy_fill_ratio": 1.0, "repeated_asset_share": 0.30, "sell_then_rebuy_ratio_decimal": 0.25, "same_hour_two_way": 10, "short_hold_ratio_decimal": 0.0}, "ACTIVE_REBALANCER"),
        ({"buy_fill_share": 0.50, "sell_to_buy_fill_ratio": 1.0, "repeated_asset_share": 0.0, "sell_then_rebuy_ratio_decimal": 0.40, "same_hour_two_way": 20, "short_hold_ratio_decimal": 0.60}, "MIXED_OR_UNCLEAR"),
        ({"buy_fill_share": 0.50, "sell_to_buy_fill_ratio": 1.0, "repeated_asset_share": 0.10, "sell_then_rebuy_ratio_decimal": 0.10, "same_hour_two_way": 1, "short_hold_ratio_decimal": 0.10}, "MIXED_OR_UNCLEAR"),
    ],
)
def test_style_classifier_uses_metrics_not_wallet_identity(metrics: dict, expected: str) -> None:
    assert analysis.classify_trader_style(metrics) == expected


def test_style_classifier_source_has_no_wallet_address_hardcoding() -> None:
    source = (SCRIPT_PATH.parents[1] / "src" / "polymarket_highest_temperature_trader_pattern_advanced.py").read_text(encoding="utf-8")
    assert "0x7c63520c2ca9b336af0c205b9ccf68217bb393d4" not in analysis.classify_trader_style.__code__.co_consts
    assert "0x8fbd7cf5f806f563080864694415829f7229a959" not in analysis.classify_trader_style.__code__.co_consts
    assert "BUY_DOMINANT_ACCUMULATOR" in source


def test_no_network_client_is_present_in_second_stage_script() -> None:
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "requests.get" not in source
    assert "urllib.request" not in source
    assert "http://" not in source
    assert "https://" not in source


def test_generated_machine_outputs_are_present_and_local_only() -> None:
    root = SCRIPT_PATH.parents[1]
    expected = [
        "SECOND_STAGE_TRADER_PATTERN_COMPARISON.json",
        "asset_path_summary.csv",
        "high_sell_path_fills.csv",
        "high_sell_path_assets.csv",
        "daily_temperature_structure.csv",
        "trader_style_metrics.csv",
    ]
    for name in expected:
        assert (root / name).exists(), name
    payload = json.loads((root / expected[0]).read_text(encoding="utf-8"))
    assert payload["network_accessed"] is False
    with (root / "high_sell_path_fills.csv").open(newline="", encoding="utf-8") as handle:
        fields = set(csv.DictReader(handle).fieldnames or [])
    assert {"observed_net_inventory_before", "sell_to_observed_inventory_ratio", "observed_exit_classification"} <= fields


def test_generated_report_uses_dynamic_style_and_exit_language() -> None:
    report = (SCRIPT_PATH.parents[1] / "SECOND_STAGE_TRADER_PATTERN_COMPARISON.md").read_text(encoding="utf-8")
    assert "DIRECTIONAL_ACCUMULATOR" not in report
    assert "大多数匹配资产" not in report
    assert "BUY_DOMINANT_ACCUMULATOR" in report
    assert "DENOMINATOR_97_EXPLANATION=" in report
