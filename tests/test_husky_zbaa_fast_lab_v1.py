from __future__ import annotations

import csv
import json
import sqlite3
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest

from src.husky_zbaa_fast_lab_v1 import (
    FORMAL_ZERO_STATUS,
    ValidationError,
    analyze_history,
    build_run_identity,
    build_signal_id,
    buckets_adjacent,
    build_portfolios,
    bucket_wins,
    decimal,
    evaluate_market,
    iter_csv_chunks,
    load_saved_evidence,
    main,
    normalize_evidence_record,
    probability_for_bucket,
    render_daily_report,
    run_shadow,
    settle_shadow,
    summarize_shadow,
    update_shadow,
    validate_probability_input,
    write_demo_ledger,
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


def _json_file(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _dated_probability(tmp_path: Path, weather_date: str, forecast_run_id: str, *, lower_tail: bool = False) -> Path:
    payload = json.loads(INPUT_PATH.read_text(encoding="utf-8"))
    day = int(weather_date[-2:])
    payload["forecast_run_id"] = forecast_run_id
    payload["weather_date_local"] = weather_date
    payload["as_of_time_cst"] = f"2026-07-{day - 1:02d}T15:00:00+08:00"
    payload["as_of_time_utc"] = f"2026-07-{day - 1:02d}T07:00:00Z"
    payload["generated_at_utc"] = f"2026-07-{day - 1:02d}T07:00:00Z"
    if lower_tail:
        by_temp = {row["temperature_c"]: row for row in payload["integer_temperature_probabilities"]}
        by_temp[25]["probability"] = 0.25
        by_temp[28]["probability"] = 0.15
    return _json_file(tmp_path / f"probability-{weather_date}.json", payload)


def _dated_evidence(
    tmp_path: Path,
    weather_date: str,
    *,
    suffix: str,
    book_mode: str = "entry",
) -> Path:
    payload = json.loads(EVIDENCE_PATH.read_text(encoding="utf-8"))
    day = int(weather_date[-2:])
    payload["evidence_label"] = f"TEST_FIXTURE_{book_mode.upper()}_{weather_date}"
    payload["captured_at_utc"] = f"2026-07-{day - 1:02d}T08:00:00Z"
    for record in payload["markets"]:
        gamma = record["gamma"]
        gamma["question"] = gamma["question"].replace("July 22", f"July {day}")
        gamma["title"] = gamma["title"].replace("July 22", f"July {day}")
        gamma["slug"] = gamma["slug"].replace("july-22-2026", f"july-{day}-2026")
        gamma["endDate"] = f"{weather_date}T12:00:00Z"
        old_condition = gamma["conditionId"]
        gamma["conditionId"] = old_condition + suffix
        tokens = json.loads(gamma["clobTokenIds"])
        tokens = [token + suffix for token in tokens]
        gamma["clobTokenIds"] = json.dumps(tokens)
        record["clob"]["condition_id"] = gamma["conditionId"]
        record["orderbook"]["market"] = gamma["conditionId"]
        record["orderbook"]["asset_id"] = tokens[0]
        record["captured_at_utc"] = payload["captured_at_utc"]
        if book_mode == "no_trigger":
            record["orderbook"]["bids"] = [{"price": "0.10", "size": "10000"}]
            record["orderbook"]["asks"] = [{"price": "0.50", "size": "10000"}]
        elif book_mode == "fake_best_bid":
            record["orderbook"]["bids"] = [
                {"price": "0.40", "size": "5"},
                {"price": "0.20", "size": "10000"},
            ]
            record["orderbook"]["asks"] = [{"price": "0.50", "size": "10000"}]
        elif book_mode == "trigger":
            record["orderbook"]["bids"] = [{"price": "0.40", "size": "10000"}]
            record["orderbook"]["asks"] = [{"price": "0.50", "size": "10000"}]
    return _json_file(tmp_path / f"evidence-{weather_date}-{book_mode}.json", payload)


def _new_run(
    tmp_path: Path,
    name: str,
    weather_date: str,
    forecast_run_id: str,
    *,
    lower_tail: bool = False,
) -> tuple[Path, Path, Path, dict]:
    probability = _dated_probability(tmp_path, weather_date, forecast_run_id, lower_tail=lower_tail)
    evidence = _dated_evidence(tmp_path, weather_date, suffix=f"-{name}")
    run_dir = tmp_path / name
    report = run_shadow(probability, Decimal("20"), run_dir, evidence)
    return run_dir, probability, evidence, report


def _ledger_rows(run_dir: Path, table: str) -> list[sqlite3.Row]:
    with sqlite3.connect(run_dir / "demo_ledger.sqlite3") as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(f"SELECT * FROM {table} ORDER BY signal_id,exit_rule").fetchall()


def test_36_run_id_binds_required_identity(validated: dict) -> None:
    identity = build_run_identity(validated)
    assert identity["forecast_run_id"] == validated["forecast_run_id"]
    assert identity["station"] == "ZBAA"
    assert identity["weather_date_local"] == "2026-07-22"
    assert identity["as_of_time_utc"] == "2026-07-21T07:00:00Z"
    assert len(identity["probability_input_sha256"]) == 64
    changed = deepcopy(validated)
    changed["forecast_run_id"] = "different"
    assert build_run_identity(changed)["run_id"] != identity["run_id"]


def test_37_different_dates_have_different_signal_ids(tmp_path: Path) -> None:
    _, _, _, first = _new_run(tmp_path, "run22", "2026-07-22", "forecast-22")
    _, _, _, second = _new_run(tmp_path, "run23", "2026-07-23", "forecast-23")
    first_id = build_signal_id(first["run_identity"]["run_id"], "EDGE_05", "MAIN_ONLY", "exact:28C")
    second_id = build_signal_id(second["run_identity"]["run_id"], "EDGE_05", "MAIN_ONLY", "exact:28C")
    assert first_id != second_id


def test_38_identical_rerun_is_idempotent_noop(tmp_path: Path) -> None:
    run_dir, probability, evidence, first = _new_run(tmp_path, "run", "2026-07-22", "forecast")
    ledger_mtime = (run_dir / "demo_ledger.sqlite3").stat().st_mtime_ns
    second = run_shadow(probability, Decimal("20"), run_dir, evidence)
    assert first["run_identity"]["run_id"] == second["run_identity"]["run_id"]
    assert second["run_status"] == "IDEMPOTENT_NOOP"
    assert (run_dir / "demo_ledger.sqlite3").stat().st_mtime_ns == ledger_mtime


def test_39_same_identity_different_content_rejected(tmp_path: Path) -> None:
    run_dir, probability, evidence, _ = _new_run(tmp_path, "run", "2026-07-22", "forecast")
    payload = json.loads(probability.read_text(encoding="utf-8"))
    payload["integer_temperature_probabilities"][0]["probability"] = 0.06
    payload["integer_temperature_probabilities"][1]["probability"] = 0.09
    conflicting = _json_file(tmp_path / "conflicting.json", payload)
    with pytest.raises(ValidationError, match="same run identity"):
        run_shadow(conflicting, Decimal("20"), run_dir, evidence)


def test_40_output_dir_other_run_rejected(tmp_path: Path) -> None:
    run_dir, _, _, _ = _new_run(tmp_path, "run", "2026-07-22", "forecast")
    probability = _dated_probability(tmp_path, "2026-07-23", "other-forecast")
    evidence = _dated_evidence(tmp_path, "2026-07-23", suffix="-other")
    with pytest.raises(ValidationError, match="another run_id"):
        run_shadow(probability, Decimal("20"), run_dir, evidence)


def test_changed_intended_usd_is_conflicting_run_content(tmp_path: Path) -> None:
    run_dir, probability, evidence, _ = _new_run(tmp_path, "run", "2026-07-22", "forecast")
    with pytest.raises(ValidationError, match="different run content"):
        run_shadow(probability, Decimal("21"), run_dir, evidence)


def test_41_ledger_cannot_be_silently_overwritten(tmp_path: Path) -> None:
    run_dir, _, _, report = _new_run(tmp_path, "run", "2026-07-22", "forecast")
    with pytest.raises(ValidationError, match="overwrite"):
        write_demo_ledger(run_dir / "demo_ledger.sqlite3", report["run_identity"], [], [])


def test_42_update_uses_bid_depth_not_asks(tmp_path: Path) -> None:
    run_dir, _, _, _ = _new_run(tmp_path, "run", "2026-07-22", "forecast")
    evidence = _dated_evidence(tmp_path, "2026-07-22", suffix="-run", book_mode="trigger")
    result = update_shadow(run_dir, evidence)
    triggered = next(item for item in result["results"] if item["status"] == "TRIGGERED")
    assert triggered["executable_sell_vwap"] == Decimal("0.40")
    assert triggered["best_ask"] == Decimal("0.50")


def test_43_best_bid_only_fake_trigger_rejected(tmp_path: Path) -> None:
    run_dir, _, _, _ = _new_run(tmp_path, "run", "2026-07-22", "forecast")
    evidence = _dated_evidence(tmp_path, "2026-07-22", suffix="-run", book_mode="fake_best_bid")
    result = update_shadow(run_dir, evidence)
    doubles = [item for item in result["results"] if item["exit_rule"] != "HOLD"]
    assert any(item["best_bid_only_would_trigger"] for item in doubles)
    assert all(item["status"] == "OPEN_NO_TRIGGER" for item in doubles)
    assert all(item["filled_sell_shares"] == 0 for item in doubles)


def test_44_depth_vwap_at_2x_triggers(tmp_path: Path) -> None:
    run_dir, _, _, _ = _new_run(tmp_path, "run", "2026-07-22", "forecast")
    evidence = _dated_evidence(tmp_path, "2026-07-22", suffix="-run", book_mode="trigger")
    result = update_shadow(run_dir, evidence)
    doubles = [item for item in result["results"] if item["exit_rule"] != "HOLD"]
    assert doubles and all(item["status"] == "TRIGGERED" for item in doubles)
    assert all(item["executable_sell_vwap"] >= item["trigger_threshold"] for item in doubles)


def test_45_double_sell_50_sells_exact_target(tmp_path: Path) -> None:
    run_dir, _, _, _ = _new_run(tmp_path, "run", "2026-07-22", "forecast")
    evidence = _dated_evidence(tmp_path, "2026-07-22", suffix="-run", book_mode="trigger")
    update_shadow(run_dir, evidence)
    row = next(row for row in _ledger_rows(run_dir, "demo_exit_experiments") if row["exit_rule"] == "DOUBLE_SELL_50")
    assert decimal(row["filled_sell_shares"]) == decimal(row["entry_shares"]) * Decimal("0.50")
    assert decimal(row["remaining_shares"]) == decimal(row["entry_shares"]) * Decimal("0.50")


def test_46_double_sell_75_sells_exact_target(tmp_path: Path) -> None:
    run_dir, _, _, _ = _new_run(tmp_path, "run", "2026-07-22", "forecast")
    evidence = _dated_evidence(tmp_path, "2026-07-22", suffix="-run", book_mode="trigger")
    update_shadow(run_dir, evidence)
    row = next(row for row in _ledger_rows(run_dir, "demo_exit_experiments") if row["exit_rule"] == "DOUBLE_SELL_75")
    assert decimal(row["filled_sell_shares"]) == decimal(row["entry_shares"]) * Decimal("0.75")
    assert decimal(row["remaining_shares"]) == decimal(row["entry_shares"]) * Decimal("0.25")


def test_47_hold_stays_open_on_update(tmp_path: Path) -> None:
    run_dir, _, _, _ = _new_run(tmp_path, "run", "2026-07-22", "forecast")
    evidence = _dated_evidence(tmp_path, "2026-07-22", suffix="-run", book_mode="trigger")
    result = update_shadow(run_dir, evidence)
    holds = [item for item in result["results"] if item["exit_rule"] == "HOLD"]
    assert holds and all(item["status"] == "OPEN" and item["filled_sell_shares"] == 0 for item in holds)


def test_48_repeated_exit_is_rejected_and_not_resold(tmp_path: Path) -> None:
    run_dir, _, _, _ = _new_run(tmp_path, "run", "2026-07-22", "forecast")
    evidence = _dated_evidence(tmp_path, "2026-07-22", suffix="-run", book_mode="trigger")
    update_shadow(run_dir, evidence)
    before = {
        (row["signal_id"], row["exit_rule"]): row["simulated_proceeds"]
        for row in _ledger_rows(run_dir, "demo_exit_experiments")
    }
    repeated = update_shadow(run_dir, evidence)
    assert all(
        item["status"] == "REPEATED_EXIT_REJECTED"
        for item in repeated["results"]
        if item["exit_rule"] != "HOLD"
    )
    after = {
        (row["signal_id"], row["exit_rule"]): row["simulated_proceeds"]
        for row in _ledger_rows(run_dir, "demo_exit_experiments")
    }
    assert after == before


def test_49_updates_append_snapshots(tmp_path: Path) -> None:
    run_dir, _, _, _ = _new_run(tmp_path, "run", "2026-07-22", "forecast")
    evidence = _dated_evidence(tmp_path, "2026-07-22", suffix="-run", book_mode="no_trigger")
    first = update_shadow(run_dir, evidence)
    second = update_shadow(run_dir, evidence)
    assert first["update_id"] != second["update_id"]
    assert len(list((run_dir / "update_snapshots").glob("*.json"))) == 2
    assert len(_ledger_rows(run_dir, "demo_update_snapshots")) == 42


def test_50_exact_settlement(tmp_path: Path) -> None:
    run_dir, _, _, _ = _new_run(tmp_path, "run", "2026-07-22", "forecast")
    result = settle_shadow(run_dir, 28)
    exact = [item for item in result["positions"] if item["temperature_bucket"] == "exact:28C"]
    assert exact and all(item["bucket_won"] for item in exact)
    assert all(item["settlement_proceeds"] > 0 for item in exact)


def test_51_lower_tail_settlement(tmp_path: Path) -> None:
    run_dir, _, _, _ = _new_run(tmp_path, "run", "2026-07-22", "forecast", lower_tail=True)
    result = settle_shadow(run_dir, 24)
    lower = [item for item in result["positions"] if item["temperature_bucket"] == "or_below:25C"]
    assert lower and all(item["bucket_won"] for item in lower)
    assert bucket_wins("or_below", Decimal("25"), 24)


def test_52_upper_tail_settlement(tmp_path: Path) -> None:
    run_dir, _, _, _ = _new_run(tmp_path, "run", "2026-07-22", "forecast")
    result = settle_shadow(run_dir, 30)
    upper = [item for item in result["positions"] if item["temperature_bucket"] == "or_higher:29C"]
    assert upper and all(item["bucket_won"] for item in upper)
    assert bucket_wins("or_higher", Decimal("29"), 30)


def test_53_partial_exit_plus_remaining_settlement(tmp_path: Path) -> None:
    run_dir, _, _, _ = _new_run(tmp_path, "run", "2026-07-22", "forecast")
    evidence = _dated_evidence(tmp_path, "2026-07-22", suffix="-run", book_mode="trigger")
    update_shadow(run_dir, evidence)
    result = settle_shadow(run_dir, 28)
    item = next(
        row for row in result["positions"]
        if row["temperature_bucket"] == "exact:28C" and row["exit_rule"] == "DOUBLE_SELL_50"
    )
    assert item["realized_exit_proceeds"] > 0
    assert item["settlement_proceeds"] > 0
    assert item["total_proceeds"] == item["realized_exit_proceeds"] + item["settlement_proceeds"]


def test_54_same_settlement_is_idempotent(tmp_path: Path) -> None:
    run_dir, _, _, _ = _new_run(tmp_path, "run", "2026-07-22", "forecast")
    settle_shadow(run_dir, 28)
    repeated = settle_shadow(run_dir, 28)
    assert repeated["settlement_status"] == "IDEMPOTENT_NOOP"


def test_55_conflicting_settlement_rejected(tmp_path: Path) -> None:
    run_dir, _, _, _ = _new_run(tmp_path, "run", "2026-07-22", "forecast")
    settle_shadow(run_dir, 28)
    with pytest.raises(ValidationError, match="conflicting settlement"):
        settle_shadow(run_dir, 29)


def test_settled_run_cannot_be_updated(tmp_path: Path) -> None:
    run_dir, _, _, _ = _new_run(tmp_path, "run", "2026-07-22", "forecast")
    settle_shadow(run_dir, 28)
    evidence = _dated_evidence(tmp_path, "2026-07-22", suffix="-run", book_mode="trigger")
    with pytest.raises(ValidationError, match="settled run"):
        update_shadow(run_dir, evidence)


def test_56_summary_reads_only_settled_runs(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    settled, _, _, _ = _new_run(root, "run22", "2026-07-22", "forecast-22")
    _new_run(root, "run23", "2026-07-23", "forecast-23")
    settle_shadow(settled, 28)
    result = summarize_shadow(root)
    assert result["settled_event_count"] == 1


def test_57_summary_aggregates_one_event_per_weather_date(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    run22, _, _, _ = _new_run(root, "run22", "2026-07-22", "forecast-22")
    run23, _, _, _ = _new_run(root, "run23", "2026-07-23", "forecast-23")
    settle_shadow(run22, 28)
    settle_shadow(run23, 28)
    result = summarize_shadow(root)
    strategy = next(
        item for item in result["strategies"]
        if (item["edge_rule"], item["portfolio_rule"], item["exit_rule"]) == ("EDGE_15", "MAIN_ONLY", "HOLD")
    )
    assert result["settled_event_count"] == 2
    assert strategy["settled_event_count"] == 2
    assert len(strategy["events"]) == 2


def test_58_summary_maximum_consecutive_losses(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    run22, _, _, _ = _new_run(root, "run22", "2026-07-22", "forecast-22")
    run23, _, _, _ = _new_run(root, "run23", "2026-07-23", "forecast-23")
    settle_shadow(run22, 20)
    settle_shadow(run23, 20)
    result = summarize_shadow(root)
    strategy = next(
        item for item in result["strategies"]
        if (item["edge_rule"], item["portfolio_rule"], item["exit_rule"]) == ("EDGE_15", "MAIN_ONLY", "HOLD")
    )
    assert strategy["maximum_consecutive_losses"] == 2
    assert strategy["losing_events"] == 2


def test_59_summary_removes_top_one_and_top_five(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    run22, _, _, _ = _new_run(root, "run22", "2026-07-22", "forecast-22")
    run23, _, _, _ = _new_run(root, "run23", "2026-07-23", "forecast-23")
    settle_shadow(run22, 28)
    settle_shadow(run23, 28)
    result = summarize_shadow(root)
    strategy = next(
        item for item in result["strategies"]
        if (item["edge_rule"], item["portfolio_rule"], item["exit_rule"]) == ("EDGE_15", "MAIN_ONLY", "HOLD")
    )
    positive = sorted((event["net_pnl"] for event in strategy["events"] if event["net_pnl"] > 0), reverse=True)
    assert strategy["pnl_without_top_1"] == strategy["total_pnl"] - sum(positive[:1], Decimal("0"))
    assert strategy["pnl_without_top_5"] == strategy["total_pnl"] - sum(positive[:5], Decimal("0"))
    assert strategy["sample_status"] == "INSUFFICIENT_FORWARD_SAMPLE"


def test_no_trade_run_can_settle_and_be_summarized(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    probability = _dated_probability(root, "2026-07-22", "no-trade")
    evidence_path = _dated_evidence(root, "2026-07-22", suffix="-no-trade")
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    for record in evidence["markets"]:
        record["orderbook"]["bids"] = [{"price": "0.80", "size": "10000"}]
        record["orderbook"]["asks"] = [{"price": "0.90", "size": "10000"}]
    _json_file(evidence_path, evidence)
    run_dir = root / "run-no-trade"
    report = run_shadow(probability, Decimal("20"), run_dir, evidence_path)
    assert report["demo_ledger"]["demo_signal_count"] == 0
    settlement = settle_shadow(run_dir, 28)
    assert settlement["positions"] == []
    summary = summarize_shadow(root)
    assert summary["settled_event_count"] == 1
    assert all(item["traded_event_count"] == 0 for item in summary["strategies"])


@pytest.mark.parametrize(
    "argv",
    [
        ["update-shadow", "--run-dir", "unused", "--saved-public-evidence", "unused", "--mode", "FORMAL"],
        ["settle-shadow", "--run-dir", "unused", "--observed-max-temp-c", "28", "--mode", "FORMAL"],
        ["summarize-shadow", "--runs-root", "unused", "--mode", "FORMAL"],
    ],
)
def test_60_formal_rejected_for_new_commands(argv: list[str]) -> None:
    assert main(argv) == 2
