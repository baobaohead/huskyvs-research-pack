from __future__ import annotations

import csv
import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest

from src.husky_zbaa_fast_lab_v1 import (
    FORMAL_ZERO_STATUS,
    ValidationError,
    analyze_history,
    buckets_adjacent,
    build_portfolios,
    evaluate_market,
    iter_csv_chunks,
    load_saved_evidence,
    main,
    normalize_evidence_record,
    probability_for_bucket,
    render_daily_report,
    run_shadow,
    validate_probability_input,
)


ROOT = Path(__file__).resolve().parents[1]
INPUT_PATH = ROOT / "templates/zbaa_shadow_probability_input_v1.json"
EVIDENCE_PATH = ROOT / "tests/fixtures/zbaa_fast_lab_saved_evidence_v1.json"


@pytest.fixture
def probability_payload() -> dict:
    return json.loads(INPUT_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def validated(probability_payload: dict) -> dict:
    return validate_probability_input(probability_payload)


@pytest.fixture
def evidence_records() -> list[dict]:
    return load_saved_evidence(EVIDENCE_PATH)[0]


@pytest.fixture
def markets(validated: dict, evidence_records: list[dict]) -> list[dict]:
    return [normalize_evidence_record(record, validated) for record in evidence_records]


def test_01_valid_zbaa_manual_input(validated: dict) -> None:
    assert validated["station"] == "ZBAA"
    assert validated["city"] == "Beijing"
    assert sum(row["probability"] for row in validated["integer_temperature_probabilities"]) == 1


def test_02_probability_sum_not_one_rejected(probability_payload: dict) -> None:
    probability_payload["integer_temperature_probabilities"][0]["probability"] = 0.04
    with pytest.raises(ValidationError, match="sum"):
        validate_probability_input(probability_payload)


def test_03_non_1500_cutoff_rejected(probability_payload: dict) -> None:
    probability_payload["as_of_time_cst"] = "2026-07-21T15:01:00+08:00"
    with pytest.raises(ValidationError, match="15:00"):
        validate_probability_input(probability_payload)


def test_04_non_next_day_rejected(probability_payload: dict) -> None:
    probability_payload["weather_date_local"] = "2026-07-23"
    with pytest.raises(ValidationError, match="next local day"):
        validate_probability_input(probability_payload)


def test_05_zspd_rejected(probability_payload: dict) -> None:
    probability_payload["station"] = "ZSPD"
    with pytest.raises(ValidationError, match="only ZBAA"):
        validate_probability_input(probability_payload)


def test_06_formal_probability_mode_rejected(probability_payload: dict) -> None:
    probability_payload["mode"] = "FORMAL"
    with pytest.raises(ValidationError, match="FORMAL"):
        validate_probability_input(probability_payload)


def test_07_post_cutoff_generation_rejected(probability_payload: dict) -> None:
    probability_payload["generated_at_utc"] = "2026-07-21T07:00:01Z"
    with pytest.raises(ValidationError, match="after"):
        validate_probability_input(probability_payload)


def test_08_duplicate_temperature_rejected(probability_payload: dict) -> None:
    probability_payload["integer_temperature_probabilities"][1]["temperature_c"] = 25
    with pytest.raises(ValidationError, match="duplicate"):
        validate_probability_input(probability_payload)


def test_09_incomplete_integer_range_rejected(probability_payload: dict) -> None:
    probability_payload["integer_temperature_probabilities"][1]["temperature_c"] = 30
    with pytest.raises(ValidationError, match="contiguous"):
        validate_probability_input(probability_payload)


def test_10_beijing_market_matches(validated: dict, evidence_records: list[dict]) -> None:
    market = normalize_evidence_record(evidence_records[2], validated)
    assert market["temperature_bucket"] == "exact:27C"


def test_11_wrong_date_market_rejected(validated: dict, evidence_records: list[dict]) -> None:
    record = deepcopy(evidence_records[2])
    record["gamma"]["question"] = "Will the highest temperature in Beijing be 27°C on July 23?"
    record["gamma"]["title"] = "Highest temperature in Beijing on July 23?"
    record["gamma"]["slug"] = "highest-temperature-in-beijing-on-july-23-2026-27c"
    record["gamma"]["endDate"] = "2026-07-23T12:00:00Z"
    with pytest.raises(ValidationError, match="date mismatch"):
        normalize_evidence_record(record, validated)


def test_12_low_temperature_market_rejected(validated: dict, evidence_records: list[dict]) -> None:
    record = deepcopy(evidence_records[2])
    record["gamma"]["question"] = "Will the lowest temperature in Beijing be 27°C on July 22?"
    record["gamma"]["title"] = "Lowest temperature in Beijing on July 22?"
    record["gamma"]["slug"] = "lowest-temperature-in-beijing-on-july-22-2026-27c"
    with pytest.raises(ValidationError, match="metric mismatch"):
        normalize_evidence_record(record, validated)


def test_13_exact_bucket_probability(validated: dict) -> None:
    assert probability_for_bucket(validated["integer_temperature_probabilities"], "exact", Decimal("28")) == Decimal("0.35")


def test_14_or_below_probability(validated: dict) -> None:
    assert probability_for_bucket(validated["integer_temperature_probabilities"], "or_below", Decimal("26")) == Decimal("0.15")


def test_15_or_higher_probability(validated: dict) -> None:
    assert probability_for_bucket(validated["integer_temperature_probabilities"], "or_higher", Decimal("29")) == Decimal("0.30")


def test_16_yes_token_binding(markets: list[dict]) -> None:
    assert {market["token_id"] for market in markets} == {"yes25below", "yes26", "yes27", "yes28", "yes29higher"}


def test_market_threshold_must_be_explicitly_covered(validated: dict) -> None:
    with pytest.raises(ValidationError, match="explicitly cover"):
        probability_for_bucket(validated["integer_temperature_probabilities"], "exact", Decimal("30"))


def test_17_empty_orderbook_handled(validated: dict, evidence_records: list[dict]) -> None:
    record = deepcopy(evidence_records[2])
    record["orderbook"]["bids"] = []
    record["orderbook"]["asks"] = []
    result = evaluate_market(normalize_evidence_record(record, validated), Decimal("20"))
    assert result["orderbook_status"] == "no_ask"
    assert result["unfilled_usd"] == Decimal("20")


def test_18_no_ask_handled(validated: dict, evidence_records: list[dict]) -> None:
    record = deepcopy(evidence_records[2])
    record["orderbook"]["asks"] = []
    result = evaluate_market(normalize_evidence_record(record, validated), Decimal("20"))
    assert result["executable_average_price"] is None
    assert result["executable_edge"] is None


def test_19_thin_book_uses_depth_weighted_average(markets: list[dict]) -> None:
    market = next(item for item in markets if item["temperature_bucket"] == "exact:28C")
    result = evaluate_market(market, Decimal("20"))
    assert result["best_ask"] == Decimal("0.14")
    assert result["executable_average_price"] > result["best_ask"]
    assert result["executable_average_price"] == Decimal("20") / (Decimal("50") + Decimal("13") / Decimal("0.16"))


def test_20_intended_usd_can_partially_fill(markets: list[dict]) -> None:
    market = next(item for item in markets if item["temperature_bucket"] == "or_higher:29C")
    result = evaluate_market(market, Decimal("20"))
    assert result["orderbook_status"] == "partial"
    assert result["executable_usd"] == Decimal("4.75")
    assert result["unfilled_usd"] == Decimal("15.25")


def test_21_executable_edge_recomputed_from_vwap(markets: list[dict]) -> None:
    market = next(item for item in markets if item["temperature_bucket"] == "exact:27C")
    result = evaluate_market(market, Decimal("20"))
    assert result["executable_edge"] == result["forecast_probability"] - result["executable_average_price"]
    assert result["executable_edge"] < result["forecast_probability"] - result["best_ask"]


def test_22_edge_05_10_15_thresholds(markets: list[dict]) -> None:
    evaluations = [evaluate_market(market, Decimal("20")) for market in markets]
    portfolios = build_portfolios(evaluations, Decimal("20"))
    assert portfolios["EDGE_05"]["eligible_buckets"] == ["exact:28C", "or_higher:29C", "exact:27C"]
    assert portfolios["EDGE_10"]["eligible_buckets"] == ["exact:28C", "or_higher:29C"]
    assert portfolios["EDGE_15"]["eligible_buckets"] == ["exact:28C"]


def test_23_main_only_selects_best_edge(markets: list[dict]) -> None:
    portfolios = build_portfolios([evaluate_market(market, Decimal("20")) for market in markets], Decimal("20"))
    assert portfolios["EDGE_05"]["MAIN_ONLY"]["allocations"][0]["temperature_bucket"] == "exact:28C"
    assert portfolios["EDGE_05"]["MAIN_ONLY"]["allocations"][0]["fraction"] == 1


def test_24_top2_adjacent_fixed_70_30(markets: list[dict]) -> None:
    portfolios = build_portfolios([evaluate_market(market, Decimal("20")) for market in markets], Decimal("20"))
    allocations = portfolios["EDGE_10"]["TOP2_ADJACENT"]["allocations"]
    assert [row["fraction"] for row in allocations] == [Decimal("0.70"), Decimal("0.30")]
    assert [row["intended_usd"] for row in allocations] == [Decimal("14"), Decimal("6")]


def test_25_nonadjacent_top_two_do_not_build_basket() -> None:
    evaluations = [
        {
            "temperature_bucket": "exact:25C", "bucket_type": "exact", "threshold_value": Decimal("25"),
            "forecast_probability": Decimal("0.5"), "executable_edge": Decimal("0.3"), "executable_usd": Decimal("20"),
        },
        {
            "temperature_bucket": "exact:27C", "bucket_type": "exact", "threshold_value": Decimal("27"),
            "forecast_probability": Decimal("0.4"), "executable_edge": Decimal("0.2"), "executable_usd": Decimal("20"),
        },
    ]
    result = build_portfolios(evaluations, Decimal("20"))
    assert result["EDGE_05"]["TOP2_ADJACENT"]["status"] == "NO_TRADE"


def test_26_tail_bucket_adjacency() -> None:
    exact = {"bucket_type": "exact", "threshold_value": Decimal("28")}
    upper = {"bucket_type": "or_higher", "threshold_value": Decimal("29")}
    assert buckets_adjacent(exact, upper)


def test_27_demo_entry_and_required_outputs(tmp_path: Path) -> None:
    output = tmp_path / "demo"
    report = run_shadow(INPUT_PATH, Decimal("20"), output, EVIDENCE_PATH)
    assert report["demo_ledger"]["demo_entry_fill_count"] > 0
    assert report["demo_ledger"]["demo_exit_experiment_count"] == report["demo_ledger"]["demo_entry_fill_count"] * 3
    assert "NO_TRADE" in report["baseline"]
    for relative in (
        "decision_report.json", "decision_report.md", "shadow_signals.csv",
        "market_snapshot.json", "orderbook_snapshots", "run_manifest.json",
    ):
        assert (output / relative).exists()


def test_28_formal_counts_stay_zero(tmp_path: Path) -> None:
    report = run_shadow(INPUT_PATH, Decimal("20"), tmp_path / "demo", EVIDENCE_PATH)
    for field, expected in FORMAL_ZERO_STATUS.items():
        assert report["safety"][field] == expected


def test_29_formal_cli_mode_rejected(tmp_path: Path) -> None:
    code = main(
        [
            "run-shadow", "--probability-input", str(INPUT_PATH), "--intended-usd", "20",
            "--output-dir", str(tmp_path / "demo"), "--saved-public-evidence", str(EVIDENCE_PATH),
            "--mode", "FORMAL",
        ]
    )
    assert code == 2
    assert not (tmp_path / "demo").exists()


def _write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _synthetic_history(root: Path) -> None:
    lifecycle_fields = [
        "asset", "city", "weather_date", "weather_metric", "bucket_label", "bucket_kind",
        "bucket_low", "bucket_high", "buy_count", "buy_shares", "buy_usd", "sell_count",
        "sell_shares", "sell_usd", "weighted_avg_buy_price", "weighted_avg_sell_price",
        "authoritative_realized_pnl", "pnl_status", "exit_mode", "first_buy_utc",
    ]
    trade_fields = ["asset", "side", "timestamp", "price"]
    pnls = [100, 10, -1, -2, -3, -4, -5, -6, -7, -8]
    lifecycle_rows = []
    trade_rows = []
    for index, pnl in enumerate(pnls, start=1):
        lifecycle_rows.append(
            {
                "asset": f"a{index}", "city": "Beijing" if index == 1 else "City",
                "weather_date": f"2026-01-{index:02d}", "weather_metric": "high",
                "bucket_label": "20C", "bucket_kind": "exact", "bucket_low": "20",
                "bucket_high": "20", "buy_count": "1", "buy_shares": "10", "buy_usd": "1",
                "sell_count": "0", "sell_shares": "0", "sell_usd": "0",
                "weighted_avg_buy_price": "0.1", "weighted_avg_sell_price": "",
                "authoritative_realized_pnl": str(pnl), "pnl_status": "closed",
                "exit_mode": "no_sell", "first_buy_utc": f"2026-01-{index:02d}T00:00:00Z",
            }
        )
        trade_rows.append({"asset": f"a{index}", "side": "BUY", "timestamp": str(index), "price": "0.1"})
    _write_csv(root / "data/processed/weather_position_lifecycle.csv", lifecycle_fields, lifecycle_rows)
    _write_csv(root / "data/processed/weather_trades_normalized.csv", trade_fields, trade_rows)


def test_30_history_aggregates_by_weather_event(tmp_path: Path) -> None:
    _synthetic_history(tmp_path)
    result = analyze_history(tmp_path)
    assert result["sample"]["weather_event_count"] == 10
    assert result["sample"]["position_count"] == 10
    assert result["sample"]["beijing_event_count"] == 1


def test_31_history_chronological_70_30_split(tmp_path: Path) -> None:
    _synthetic_history(tmp_path)
    split = analyze_history(tmp_path)["sample"]["time_split"]
    assert split["train_event_count"] == 7
    assert split["validation_event_count"] == 3


def test_32_top_event_concentration(tmp_path: Path) -> None:
    _synthetic_history(tmp_path)
    pnl = analyze_history(tmp_path)["statistics"]["event_pnl"]
    assert pnl["top_event_share_of_gross_positive"] == pytest.approx(100 / 110)


def test_33_remove_top_winners(tmp_path: Path) -> None:
    _synthetic_history(tmp_path)
    pnl = analyze_history(tmp_path)["statistics"]["event_pnl"]
    assert pnl["total"] == 74
    assert pnl["remove_top_1"] == -26
    assert pnl["remove_top_5"] == -36


def test_34_large_csv_reader_is_chunked(tmp_path: Path) -> None:
    path = tmp_path / "large.csv"
    _write_csv(path, ["a", "b"], [{"a": index, "b": index * 2} for index in range(23)])
    chunks = list(iter_csv_chunks(path, {"a"}, chunk_size=5))
    assert [len(chunk) for chunk in chunks] == [5, 5, 5, 5, 3]
    assert set(chunks[0][0]) == {"a"}


def test_35_daily_report_is_plain_language(markets: list[dict], validated: dict) -> None:
    evaluations = [evaluate_market(market, Decimal("20")) for market in markets]
    portfolios = build_portfolios(evaluations, Decimal("20"))
    report = render_daily_report(validated, evaluations, portfolios, [], {"evidence_label": "FIXTURE"})
    for phrase in ("一、天气判断", "二、市场判断", "三、模拟动作", "四、退出实验", "继续观察", "五、风险提醒"):
        assert phrase in report
