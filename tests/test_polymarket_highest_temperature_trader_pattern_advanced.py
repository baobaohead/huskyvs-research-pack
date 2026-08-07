from __future__ import annotations

import importlib.util
import json
import sys
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
