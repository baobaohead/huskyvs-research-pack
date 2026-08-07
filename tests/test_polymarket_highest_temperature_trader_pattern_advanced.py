from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import src.polymarket_highest_temperature_trader_pattern_advanced as advanced
import src.polymarket_highest_temperature_trader_pattern_v1 as study
import scripts.second_stage_trader_pattern_analysis as compatibility


SKILL_RUNNER = ROOT / "skills/polymarket-highest-temperature-trader-pattern-v1/scripts/run_analysis.py"
PORTABLE = ROOT / "docs/husky_beijing_full_trade_study_v1/saved_evidence_v1/manifest.json"
EXAMPLE = ROOT / "skills/polymarket-highest-temperature-trader-pattern-v1/examples/example_input.yaml"


def synthetic_row(
    city: str,
    weather_date: str,
    offset_seconds: int,
    *,
    side: str = "BUY",
    outcome: str = "YES",
    price: float = 0.20,
    shares: float = 100.0,
    condition_id: str = "condition-1",
    asset: str = "asset-1",
    temperature_bucket: str = "30",
    event_slug: str | None = None,
) -> dict[str, str]:
    local_dt = datetime.fromisoformat(f"{weather_date}T00:00:00") + timedelta(seconds=offset_seconds)
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
        "canonical_city": city,
        "condition_id": condition_id,
        "asset": asset,
        "temperature_bucket": temperature_bucket,
        "bucket_kind": "exact",
        "bucket_low": temperature_bucket,
        "bucket_high": temperature_bucket,
        "event_slug": event_slug or f"highest-temperature-in-{city}-on-{weather_date}",
        "transaction_hash": f"tx-{city}-{weather_date}-{offset_seconds}-{side}-{outcome}-{asset}",
        "relative_weather_day": "D0",
        "report_time_bucket": "D0_00_08",
    }


def synthetic_event(city: str, weather_date: str, suffix: str = "") -> dict[str, object]:
    slug = f"highest-temperature-in-{city}-on-{weather_date}{suffix}"
    return {
        "canonical_city": city,
        "weather_date": weather_date,
        "event_id": f"event-{city}-{weather_date}{suffix}",
        "event_slug": slug,
        "condition_count": 1,
        "completeness_status": "COMPLETE",
    }


def synthetic_wallet_data(rows: list[dict[str, str]], cities: list[str]) -> dict[str, object]:
    weather_dates = sorted({row["weather_date_local"] for row in rows})
    events = [synthetic_event(city, day) for city in cities for day in weather_dates]
    return advanced.analyze_wallet_rows(
        rows,
        events,
        [date.fromisoformat(day) for day in weather_dates],
        {"pattern_report_status": "READY"},
        cities,
    )


def load_runner():
    spec = importlib.util.spec_from_file_location("advanced_skill_runner", SKILL_RUNNER)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_compatibility_entry_is_thin_and_reuses_formal_core() -> None:
    source = (ROOT / "scripts/second_stage_trader_pattern_analysis.py").read_text(encoding="utf-8")
    assert "def asset_paths" not in source
    assert "polymarket_highest_temperature_trader_pattern_advanced" in source
    assert compatibility.make_report is advanced.make_report


def test_runner_routes_basic_by_default_and_advanced_explicitly() -> None:
    runner = load_runner()
    payload = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    basic = runner.build_command(payload, Path("/tmp/out-basic"), refresh_public_data=False, saved_public_evidence_manifest=PORTABLE)
    assert ["--analysis-depth", "basic"] == basic[basic.index("--analysis-depth"):basic.index("--analysis-depth") + 2]
    payload["analysis_depth"] = "advanced"
    advanced_command = runner.build_command(payload, Path("/tmp/out-advanced"), refresh_public_data=False, saved_public_evidence_manifest=PORTABLE, analysis_depth="advanced")
    assert ["--analysis-depth", "advanced"] == advanced_command[advanced_command.index("--analysis-depth"):advanced_command.index("--analysis-depth") + 2]


def test_basic_output_is_unchanged_and_advanced_adds_only_new_files(tmp_path: Path) -> None:
    wallet = "0xaf17116ae2b1476032785a67bd5b7c8c05905c20"
    basic_root = tmp_path / "basic"
    advanced_root = tmp_path / "advanced"
    basic_result = study.analyze([wallet], "2026-03-21", "2026-07-23", ["beijing"], basic_root, saved_public_evidence_manifest=PORTABLE, analysis_depth="basic")
    advanced_result = study.analyze([wallet], "2026-03-21", "2026-07-23", ["beijing"], advanced_root, saved_public_evidence_manifest=PORTABLE, analysis_depth="advanced")
    basic_summary = json.loads((basic_root / wallet / "summary.json").read_text(encoding="utf-8"))
    advanced_basic_summary = json.loads((advanced_root / wallet / "summary.json").read_text(encoding="utf-8"))
    assert basic_result.keys() == {"run_manifest", "summaries", "comparison"}
    assert "advanced" in advanced_result
    assert basic_summary == advanced_basic_summary
    assert not list((basic_root / wallet).glob("advanced_*") )
    assert (advanced_root / wallet / "advanced_summary.md").is_file()
    assert (advanced_root / wallet / "advanced_summary.json").is_file()
    advanced_summary = json.loads((advanced_root / wallet / "advanced_summary.json").read_text(encoding="utf-8"))
    assert advanced_summary["analysis_depth"] == "advanced"
    for name in ("asset_path_summary.csv", "high_sell_path_fills.csv", "high_sell_path_assets.csv", "daily_temperature_structure.csv", "trader_style_metrics.csv"):
        assert (advanced_root / wallet / name).is_file(), name
    assert (advanced_root / "advanced_trader_comparison.md").is_file()
    assert (advanced_root / "advanced_trader_comparison.json").is_file()


def test_no_maker_taker_data_never_gets_market_maker_style_label() -> None:
    metrics = {
        "buy_fill_share": 0.50,
        "sell_to_buy_fill_ratio": 1.0,
        "repeated_asset_share": 0.0,
        "sell_then_rebuy_ratio_decimal": 0.40,
        "same_hour_two_way": 20,
        "short_hold_ratio_decimal": 0.60,
    }
    assert advanced.classify_trader_style(metrics) == "MIXED_OR_UNCLEAR"
    assert advanced.market_maker_like_activity(metrics) is True
    assert "POSSIBLE_MARKET_MAKER" not in advanced.classify_trader_style.__code__.co_consts


def test_reviewed_two_wallet_regression_numbers_remain_stable() -> None:
    payload = json.loads((ROOT / "SECOND_STAGE_TRADER_PATTERN_COMPARISON.json").read_text(encoding="utf-8"))
    wallet_one = payload["wallets"]["0x7c63520c2ca9b336af0c205b9ccf68217bb393d4"]
    wallet_two = payload["wallets"]["0x8fbd7cf5f806f563080864694415829f7229a959"]
    low = wallet_two["high_sell_summary"]["low_buy_high_sell"]
    assert wallet_one["style"]["style_label"] == "ACTIVE_REBALANCER"
    assert wallet_two["style"]["style_label"] == "BUY_DOMINANT_ACCUMULATOR"
    assert (low["assets"], low["dates"], low["fills"]) == (23, 23, 40)
    assert (low["partial_fill_count"], low["near_full_fill_count"], low["exceeds_fill_count"], low["unknown_fill_count"]) == (26, 14, 0, 0)
    assert (low["all_partial_asset_count"], low["near_full_asset_count"], low["mixed_asset_count"]) == (9, 6, 8)
    assert low["low_0_30_buy_weighted_price"] == pytest.approx(0.1134624321)
    assert low["high_90_100_sell_weighted_price"] == pytest.approx(0.9975689268)


def test_advanced_summary_is_chinese_public_fill_only(tmp_path: Path) -> None:
    wallet = "0xaf17116ae2b1476032785a67bd5b7c8c05905c20"
    study.analyze([wallet], "2026-03-21", "2026-07-23", ["beijing"], tmp_path, saved_public_evidence_manifest=PORTABLE, analysis_depth="advanced")
    report = (tmp_path / wallet / "advanced_summary.md").read_text(encoding="utf-8")
    assert "先说结论" in report
    assert "公开成交" in report
    assert "PnL" in report
    assert "真实库存" not in report


def test_event_records_retain_canonical_city() -> None:
    result = advanced.event_records_from_payloads(
        [{"event_id": "", "event_slug": "slug-b", "weather_date_local": "2026-05-01", "canonical_city": "beijing"}],
        [{"event_id": "event-b", "event_slug": "slug-b", "weather_date_local": "2026-05-01", "canonical_city": "beijing"}],
    )
    assert result[0]["canonical_city"] == "beijing"


def test_same_calendar_date_in_two_cities_is_two_city_weather_days() -> None:
    events = [synthetic_event("beijing", "2026-05-01"), synthetic_event("shanghai", "2026-05-01")]
    result = advanced.date_denominator(Path("/unused"), date(2026, 5, 1), date(2026, 5, 1), events, ["beijing", "shanghai"])
    assert result["unique_calendar_date_count"] == 1
    assert result["city_weather_day_count"] == 2
    assert result["event_count"] == 2
    assert result["duplicate_city_weather_day_count"] == 0


def test_same_city_calendar_date_is_duplicate_city_weather_day() -> None:
    events = [synthetic_event("beijing", "2026-05-01"), synthetic_event("beijing", "2026-05-01", "-arch")]
    result = advanced.date_denominator(Path("/unused"), date(2026, 5, 1), date(2026, 5, 1), events, ["beijing"])
    assert result["unique_calendar_date_count"] == 1
    assert result["city_weather_day_count"] == 1
    assert result["duplicate_city_weather_day_count"] == 1


def test_out_of_range_city_event_is_excluded() -> None:
    events = [synthetic_event("beijing", "2026-05-01"), synthetic_event("shanghai", "2026-05-02")]
    result = advanced.date_denominator(Path("/unused"), date(2026, 5, 1), date(2026, 5, 1), events)
    assert result["event_count"] == 1
    assert result["out_of_range_event_count"] == 1


def test_asset_path_key_contains_city_and_does_not_merge_cities() -> None:
    rows = [
        synthetic_row("beijing", "2026-05-01", 0),
        synthetic_row("shanghai", "2026-05-01", 1),
    ]
    paths = advanced.asset_paths(advanced.normalize_fills(rows))
    assert len(paths) == 2
    assert {path["canonical_city"] for path in paths} == {"beijing", "shanghai"}
    assert all(path["key"][0] in {"beijing", "shanghai"} for path in paths)


def test_temperature_days_do_not_merge_same_date_across_cities() -> None:
    rows = [
        synthetic_row("beijing", "2026-05-01", 0, temperature_bucket="30"),
        synthetic_row("shanghai", "2026-05-01", 1, temperature_bucket="31"),
    ]
    data = synthetic_wallet_data(rows, ["beijing", "shanghai"])
    assert len(data["temperature_days"]) == 2
    assert {day["canonical_city"] for day in data["temperature_days"]} == {"beijing", "shanghai"}
    assert data["multi_yes"]["multi_yes_days"] == 0
    assert data["category_counts"]["SINGLE_YES_ONLY"] == 2


def test_multi_yes_is_counted_per_city_weather_day() -> None:
    rows = [
        synthetic_row("beijing", "2026-05-01", 0, temperature_bucket="30"),
        synthetic_row("beijing", "2026-05-01", 1, temperature_bucket="31"),
        synthetic_row("shanghai", "2026-05-01", 2, temperature_bucket="30"),
    ]
    data = synthetic_wallet_data(rows, ["beijing", "shanghai"])
    assert data["multi_yes"]["multi_yes_days"] == 1
    assert data["category_counts"]["MULTI_YES_ONLY"] == 1
    assert data["category_counts"]["SINGLE_YES_ONLY"] == 1


def test_no_buy_on_one_city_does_not_make_other_city_mixed() -> None:
    rows = [
        synthetic_row("beijing", "2026-05-01", 0, outcome="YES"),
        synthetic_row("shanghai", "2026-05-01", 1, outcome="NO"),
    ]
    data = synthetic_wallet_data(rows, ["beijing", "shanghai"])
    assert data["no"]["mixed_days"] == 0
    assert data["category_counts"]["SINGLE_YES_ONLY"] == 1
    assert data["category_counts"]["SINGLE_NO_ONLY"] == 1


def test_wallet_style_uses_city_weather_day_denominator_and_labels_top_days() -> None:
    rows = [
        synthetic_row("beijing", "2026-05-01", 0),
        synthetic_row("shanghai", "2026-05-01", 1),
    ]
    data = synthetic_wallet_data(rows, ["beijing", "shanghai"])
    assert data["style"]["active_days"] == 2
    assert data["style"]["average_fills_per_active_day"] == pytest.approx(1.0)
    assert {row["canonical_city"] for row in data["style"]["top_days"]} == {"beijing", "shanghai"}


def test_high_sell_daily_cases_keep_city_dimension() -> None:
    rows = [
        synthetic_row("beijing", "2026-05-01", 0, price=0.20),
        synthetic_row("beijing", "2026-05-01", 3600, side="SELL", price=0.99, shares=20),
        synthetic_row("shanghai", "2026-05-01", 7200, price=0.20),
        synthetic_row("shanghai", "2026-05-01", 10800, side="SELL", price=0.99, shares=20),
    ]
    data = synthetic_wallet_data(rows, ["beijing", "shanghai"])
    high = advanced.high_sell_analysis(data["paths"])
    assert high["low_buy_high_sell"]["dates"] == 2
    assert {case["canonical_city"] for case in high["daily_cases"]} == {"beijing", "shanghai"}


@pytest.mark.parametrize("key", ("BUY YES", "BUY NO", "SELL YES", "SELL NO"))
def test_empty_category_has_no_dominant_price_band(key: str) -> None:
    data = synthetic_wallet_data([], ["beijing"])
    assert all(row["fills"] == 0 for row in data["price"][key])
    assert advanced.dominant_price_row(data["price"][key]) is None


def test_empty_categories_have_no_dominant_d0_time() -> None:
    rows = advanced.side_outcome_time_table([])
    assert advanced.dominant_d0_time_row(rows) is None
    assert advanced.dominant_time_label(rows) == "未观察到"


def test_empty_category_chinese_summary_says_not_observed() -> None:
    data = synthetic_wallet_data([], ["beijing"])
    events: list[dict[str, object]] = []
    denominator = advanced.date_denominator(Path("/unused"), date(2026, 5, 1), date(2026, 5, 1), events, ["beijing"])
    report = advanced.render_wallet_summary(
        "wallet-zero", date(2026, 5, 1), date(2026, 5, 1), "beijing", [date(2026, 5, 1)], denominator, data, advanced.high_sell_analysis([]),
    )
    assert "当前公开证据未观察到BUY YES，因此无法判断BUY YES主要价格和主要成交时段。" in report
    assert "BUY YES主要成交时段为未观察到" in report


def test_empty_category_is_null_in_comparison_json(tmp_path: Path) -> None:
    data = {"wallet-zero": synthetic_wallet_data([], ["beijing"])}
    events: list[dict[str, object]] = []
    denominator = advanced.date_denominator(Path("/unused"), date(2026, 5, 1), date(2026, 5, 1), events, ["beijing"])
    payload = advanced.advanced_comparison_payload(
        date(2026, 5, 1), date(2026, 5, 1), "beijing", [date(2026, 5, 1)], denominator, data, {"wallet-zero": advanced.high_sell_analysis([])},
    )
    assert payload["wallets"]["wallet-zero"]["dominant_price_bands"]["BUY YES"] is None
    assert payload["wallets"]["wallet-zero"]["dominant_d0_time_buckets"]["BUY YES"] is None


def test_daily_machine_output_contains_city_weather_key(tmp_path: Path) -> None:
    rows = [synthetic_row("beijing", "2026-05-01", 0), synthetic_row("shanghai", "2026-05-01", 1)]
    data = {"wallet-a": synthetic_wallet_data(rows, ["beijing", "shanghai"])}
    events = [synthetic_event("beijing", "2026-05-01"), synthetic_event("shanghai", "2026-05-01")]
    denominator = advanced.date_denominator(Path("/unused"), date(2026, 5, 1), date(2026, 5, 1), events, ["beijing", "shanghai"])
    output = tmp_path / "machine"
    advanced.build_machine_outputs(output, date(2026, 5, 1), date(2026, 5, 1), "all-cities", [date(2026, 5, 1)], events, denominator, data, {"wallet-a": advanced.high_sell_analysis(data["wallet-a"]["paths"])}, ["wallet-a"])
    rows_out = (output / "daily_temperature_structure.csv").read_text(encoding="utf-8")
    assert "canonical_city" in rows_out.splitlines()[0]
    assert "beijing" in rows_out and "shanghai" in rows_out


def test_run_advanced_single_wallet_is_city_aware(tmp_path: Path) -> None:
    rows = {"wallet-a": [synthetic_row("beijing", "2026-05-01", 0), synthetic_row("shanghai", "2026-05-01", 1)]}
    result = advanced.run_advanced_analysis(
        ["wallet-a"], rows,
        [synthetic_event("beijing", "2026-05-01"), synthetic_event("shanghai", "2026-05-01")],
        tmp_path, date(2026, 5, 1), date(2026, 5, 1), "all-cities", {"wallet-a": {}}, [],
    )
    assert result["denominator"]["city_weather_day_count"] == 2
    assert len(result["wallet_data"]["wallet-a"]["temperature_days"]) == 2


def test_run_advanced_two_wallets_keeps_city_paths_separate(tmp_path: Path) -> None:
    rows = {
        "wallet-a": [synthetic_row("beijing", "2026-05-01", 0)],
        "wallet-b": [synthetic_row("shanghai", "2026-05-01", 0)],
    }
    result = advanced.run_advanced_analysis(
        ["wallet-a", "wallet-b"], rows,
        [synthetic_event("beijing", "2026-05-01"), synthetic_event("shanghai", "2026-05-01")],
        tmp_path, date(2026, 5, 1), date(2026, 5, 1), "all-cities", {"wallet-a": {}, "wallet-b": {}}, [],
    )
    assert result["denominator"]["city_weather_day_count"] == 2
    assert result["wallet_data"]["wallet-a"]["paths"][0]["canonical_city"] == "beijing"
    assert result["wallet_data"]["wallet-b"]["paths"][0]["canonical_city"] == "shanghai"


def test_advanced_comparison_supports_three_wallets_and_lists_cities(tmp_path: Path) -> None:
    wallets = ["wallet-a", "wallet-b", "wallet-c"]
    rows = {wallet: [synthetic_row(city, "2026-05-01", 0)] for wallet, city in zip(wallets, ["beijing", "shanghai", "tokyo"])}
    events = [synthetic_event(city, "2026-05-01") for city in ("beijing", "shanghai", "tokyo")]
    result = advanced.run_advanced_analysis(
        wallets, rows, events, tmp_path, date(2026, 5, 1), date(2026, 5, 1), "all-cities", {wallet: {} for wallet in wallets}, [],
    )
    comparison = json.loads((tmp_path / "advanced_trader_comparison.json").read_text(encoding="utf-8"))
    assert len(comparison["wallets"]) == 3
    assert comparison["scope"]["cities"] == ["beijing", "shanghai", "tokyo"]
    assert result["denominator"]["city_weather_day_count"] == 3


def test_requested_city_filter_does_not_add_unrequested_temperature_days() -> None:
    rows = [synthetic_row("beijing", "2026-05-01", 0), synthetic_row("shanghai", "2026-05-01", 1)]
    data = advanced.analyze_wallet_rows(rows, [], [date(2026, 5, 1)], {}, ["beijing"])
    assert len(data["temperature_days"]) == 1
    assert data["temperature_days"][0]["canonical_city"] == "beijing"


def test_basic_regression_summary_remains_unchanged_with_advanced_city_support(tmp_path: Path) -> None:
    wallet = "0xaf17116ae2b1476032785a67bd5b7c8c05905c20"
    basic = study.analyze([wallet], "2026-03-21", "2026-07-23", ["beijing"], tmp_path / "basic", saved_public_evidence_manifest=PORTABLE, analysis_depth="basic")
    advanced_result = study.analyze([wallet], "2026-03-21", "2026-07-23", ["beijing"], tmp_path / "advanced", saved_public_evidence_manifest=PORTABLE, analysis_depth="advanced")
    assert basic["summaries"] == advanced_result["summaries"]
