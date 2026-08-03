from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills/polymarket-highest-temperature-trader-pattern-v1"
RUNNER = SKILL / "scripts/run_analysis.py"
EXAMPLES = SKILL / "examples"
PORTABLE = ROOT / "docs/husky_beijing_full_trade_study_v1/saved_evidence_v1/manifest.json"
TMP = Path("/tmp/polymarket_highest_temperature_trader_pattern_v1/skill_tests")


def load_runner():
    spec = importlib.util.spec_from_file_location("highest_temp_skill_runner", RUNNER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def evidence(root: Path, wallet: str, city: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    row = {
        "proxyWallet": wallet,
        "eventSlug": f"highest-temperature-in-{city}-on-july-20-2026",
        "slug": f"highest-temperature-in-{city}-on-july-20-2026-31c",
        "title": f"Will the highest temperature in {city.title()} be 31°C on July 20?",
        "conditionId": f"condition-{wallet[-2:]}-{city}",
        "asset": f"asset-{wallet[-2:]}-{city}",
        "outcome": "Yes",
        "side": "BUY",
        "price": 0.2,
        "size": 10,
        "timestamp": 1784512800,
        "transactionHash": f"0x{wallet[-2:]}{city}",
    }
    aggregates = {}
    for name, payload in (("trades", [row]), ("activity", [{**row, "type": "TRADE", "usdcSize": 2}])):
        path = root / f"{name}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        aggregates[name] = {"relative_path": path.name, "record_count": 1, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    manifest = {
        "schema_version": "polymarket_highest_temperature_public_evidence_v1",
        "wallet": wallet,
        "weather_date_from": "2026-07-01",
        "weather_date_to": "2026-07-31",
        "collection_start_utc": "2026-06-28T00:00:00+00:00",
        "collection_end_utc": "2026-08-03T00:00:00+00:00",
        "public_data_only": True, "public_get_only": True,
        "account_connection": False, "signing": False, "real_order": False,
        "pagination_saturation_status": "COMPLETE", "requests": [], "aggregates": aggregates,
    }
    path = root / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_skill_package_required_structure():
    assert (SKILL / "SKILL.md").is_file()
    assert (SKILL / "agents/openai.yaml").is_file()
    assert RUNNER.is_file()
    assert (EXAMPLES / "example_input.yaml").is_file()
    assert (EXAMPLES / "example_output.md").is_file()


def test_skill_frontmatter_has_only_required_keys():
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    frontmatter = text.split("---", 2)[1]
    keys = {line.split(":", 1)[0] for line in frontmatter.splitlines() if ":" in line}
    assert keys == {"name", "description"}
    assert "polymarket-highest-temperature-trader-pattern-v1" in frontmatter


def test_skill_documents_applicable_and_inapplicable_requests():
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8").lower()
    for phrase in ("one or more", "weather-date", "filtering cities", "comparing traders", "yes/no", "buy/sell"):
        assert phrase in text
    for phrase in ("pnl", "roi", "real trading", "unfilled orders", "negative risk"):
        assert phrase in text


def test_skill_routes_new_wallets_to_public_refresh_and_limits_bundled_evidence():
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8").lower()
    assert "for a new wallet" in text
    assert "--refresh-public-data" in text
    assert "belongs only to `0xaf17116ae2b1476032785a67bd5b7c8c05905c20`" in text
    assert "never substitute evidence from another wallet" in text
    assert "do not create a `blocked.md`" in text


def test_skill_calls_fixed_python_module_without_statistics_logic():
    text = RUNNER.read_text(encoding="utf-8")
    assert "src.polymarket_highest_temperature_trader_pattern_v1" in text
    assert "subprocess.run" in text
    assert "price_band" not in text
    assert "cumulative_shares" not in text


def test_agents_metadata_has_required_interface_values():
    text = (SKILL / "agents/openai.yaml").read_text(encoding="utf-8")
    assert 'display_name: "Polymarket Highest Temperature Trader Pattern"' in text
    assert "short_description:" in text
    assert "$polymarket-highest-temperature-trader-pattern-v1" in text


@pytest.mark.parametrize("name", ["example_input.yaml", "example_multi_wallet.yaml", "example_all_cities.yaml"])
def test_example_inputs_are_json_compatible_yaml(name):
    payload = json.loads((EXAMPLES / name).read_text(encoding="utf-8"))
    assert payload["trader_ids"]
    assert payload["date_from"] <= payload["date_to"]
    assert isinstance(payload.get("cities", []), list)


def test_runner_builds_single_wallet_city_command():
    runner = load_runner()
    payload = runner.load_input(EXAMPLES / "example_input.yaml")
    command = runner.build_command(payload, TMP / "single", refresh_public_data=False, saved_public_evidence_manifest=PORTABLE)
    assert command.count("--wallet") == 1
    assert command[command.index("--city") + 1] == "beijing"
    assert "--saved-public-evidence-manifest" in command


def test_runner_builds_multi_wallet_command_without_mixing():
    runner = load_runner()
    payload = runner.load_input(EXAMPLES / "example_multi_wallet.yaml")
    command = runner.build_command(payload, TMP / "multi", refresh_public_data=True, saved_public_evidence_manifest=None)
    assert command.count("--wallet") == 2
    assert command.count("--city") == 2
    assert "--refresh-public-data" in command


def test_runner_all_cities_omits_city_flags():
    runner = load_runner()
    payload = runner.load_input(EXAMPLES / "example_all_cities.yaml")
    command = runner.build_command(payload, TMP / "all", refresh_public_data=True, saved_public_evidence_manifest=None)
    assert "--city" not in command


def test_runner_requires_exactly_one_evidence_mode():
    runner = load_runner()
    payload = runner.load_input(EXAMPLES / "example_input.yaml")
    with pytest.raises(ValueError):
        runner.build_command(payload, TMP, refresh_public_data=False, saved_public_evidence_manifest=None)


def test_single_wallet_example_runs_offline_and_fields_are_complete():
    output = TMP / "single_run"
    env = {**os.environ, "POLYMARKET_PUBLIC_RESEARCH_NO_NETWORK": "1"}
    completed = subprocess.run([
        sys.executable, str(RUNNER), "--input", str(EXAMPLES / "example_input.yaml"),
        "--output-root", str(output), "--saved-public-evidence-manifest", str(PORTABLE),
    ], cwd=ROOT, env=env, check=False)
    assert completed.returncode == 0
    summary = json.loads((output / "0xaf17116ae2b1476032785a67bd5b7c8c05905c20/summary.json").read_text())
    required = {
        "wallet", "weather_date_from", "weather_date_to", "discovered_cities",
        "weather_event_count", "total_public_fill_count", "buy_yes_fill_count",
        "buy_no_fill_count", "sell_yes_fill_count", "sell_no_fill_count",
        "main_relative_weather_day_by_usd", "main_d0_bucket_by_usd",
        "multi_yes_event_count", "adjacent_yes_event_count", "data_quality",
    }
    assert required <= summary.keys()
    assert summary["total_public_fill_count"] == 537


def test_multi_wallet_example_shape_runs_offline(tmp_path):
    payload = json.loads((EXAMPLES / "example_multi_wallet.yaml").read_text())
    payload["date_from"] = "2026-07-01"
    payload["date_to"] = "2026-07-31"
    input_path = tmp_path / "multi.json"
    input_path.write_text(json.dumps(payload))
    evidence_root = tmp_path / "evidence"
    children = []
    for index, wallet in enumerate(payload["trader_ids"]):
        child = evidence(evidence_root / wallet, wallet, "beijing" if index == 0 else "shanghai")
        children.append(str(child.relative_to(evidence_root)))
    root_manifest = evidence_root / "manifest.json"
    root_manifest.write_text(json.dumps({
        "schema_version": "polymarket_highest_temperature_public_evidence_v1_multi_wallet",
        "public_data_only": True, "public_get_only": True,
        "account_connection": False, "signing": False, "real_order": False,
        "wallet_manifests": children,
    }))
    output = TMP / "multi_run"
    completed = subprocess.run([
        sys.executable, str(RUNNER), "--input", str(input_path), "--output-root", str(output),
        "--saved-public-evidence-manifest", str(root_manifest),
    ], cwd=ROOT, env={**os.environ, "POLYMARKET_PUBLIC_RESEARCH_NO_NETWORK": "1"}, check=False)
    assert completed.returncode == 0
    comparison = list(__import__("csv").DictReader((output / "trader_comparison.csv").open()))
    assert {row["wallet"] for row in comparison} == set(payload["trader_ids"])


def test_all_cities_example_shape_runs_offline(tmp_path):
    payload = json.loads((EXAMPLES / "example_all_cities.yaml").read_text())
    wallet = payload["trader_ids"][0]
    manifest = evidence(tmp_path / "evidence", wallet, "beijing")
    output = TMP / "all_cities_run"
    completed = subprocess.run([
        sys.executable, str(RUNNER), "--input", str(EXAMPLES / "example_all_cities.yaml"),
        "--output-root", str(output), "--saved-public-evidence-manifest", str(manifest),
    ], cwd=ROOT, env={**os.environ, "POLYMARKET_PUBLIC_RESEARCH_NO_NETWORK": "1"}, check=False)
    assert completed.returncode == 0
    summary = json.loads((output / wallet / "summary.json").read_text())
    assert summary["all_cities_default"] is True
    assert summary["discovered_cities"] == ["beijing"]


def test_example_output_states_public_fill_limitations():
    text = (EXAMPLES / "example_output.md").read_text(encoding="utf-8").lower()
    assert "public fills are not original orders" in text
    assert "no pnl" in text and "roi" in text
