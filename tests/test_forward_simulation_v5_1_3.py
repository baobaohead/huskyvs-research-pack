import csv
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.forward_simulation_v5_1_3 import (  # noqa: E402
    DEMO,
    STRATEGY_IDS,
    audit_integrity,
    connect,
    data_dir,
    db_path,
    demo_fixture,
    demo_run,
    init_ledger,
    load_config,
    monitor_once,
    record_snapshot,
    register_signals,
    status,
)
from src.polymarket_public_adapter_v5_1_3 import (  # noqa: E402
    AdapterError,
    PublicAdapter,
    calculate_fee,
    consume_buy_depth,
    consume_sell_depth,
    dec,
    extract_fee_policy,
    gamma_token_pairs,
    normalize_orderbook,
    parse_settlement_evidence,
    validate_token_mapping,
)


CONFIG = PROJECT_ROOT / "config/forward_simulation_v5_1_3.yaml"


def market(**extra):
    m = {
        "question": "Highest temperature in Demo City on January 2?",
        "title": "Highest temperature in Demo City on January 2?",
        "slug": "highest-temperature-in-demo-city-on-january-2-2099-30c",
        "conditionId": "0xdemo",
        "outcomes": json.dumps(["Yes", "No"]),
        "clobTokenIds": json.dumps(["yes-token", "no-token"]),
        "outcomePrices": json.dumps(["0.50", "0.50"]),
        "active": True,
        "closed": False,
        "resolved": False,
        "feesEnabled": True,
        "feeSchedule": {"rate": "0.05", "exponent": "1"},
        "endDate": "2099-01-02T23:59:00Z",
        "groupItemTitle": "30C",
        "acceptingOrders": True,
    }
    m.update(extra)
    return m


def clob(**extra):
    c = {"condition_id": "0xdemo", "t": [{"t": "yes-token", "o": "Yes"}, {"t": "no-token", "o": "No"}], "fd": {"r": "0.05", "e": "1", "to": True}}
    c.update(extra)
    return c


def book(**extra):
    b = {
        "market": "0xdemo",
        "asset_id": "yes-token",
        "timestamp": "1",
        "hash": "h1",
        "bids": [{"price": "0.30", "size": "100"}],
        "asks": [{"price": "0.30", "size": "100"}],
        "min_order_size": "5",
        "tick_size": "0.001",
        "neg_risk": False,
    }
    b.update(extra)
    return b


def signal_row(**extra):
    s = {
        "signal_id": "sig1",
        "created_at_utc": "2099-01-01T00:00:00+00:00",
        "city": "Demo City",
        "weather_date_local": "2099-01-02",
        "weather_metric": "high",
        "temperature_bucket": "30C",
        "market_slug": "highest-temperature-in-demo-city-on-january-2-2099-30c",
        "condition_id": "0xdemo",
        "token_id": "yes-token",
        "outcome": "Yes",
        "side": "BUY",
        "forecast_temperature": "30",
        "forecast_probability": "0.60",
        "market_probability_at_signal": "0.30",
        "intended_usd": "30",
        "max_entry_price": "0.31",
        "source": "pytest",
        "notes": "",
    }
    s.update(extra)
    return s


def write_signals(path: Path, rows):
    fields = list(signal_row().keys())
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def test_buy_fee_adds_to_cost():
    policy = {"fee_status": "official", "fee_rate": Decimal("0.05")}
    calc = calculate_fee("buy", Decimal("10"), Decimal("0.30"), policy)
    assert calc["gross_notional"] == Decimal("3.00")
    assert calc["official_fee"] == Decimal("0.10500")
    assert calc["net_cost_or_proceeds"] == Decimal("3.10500")


def test_sell_fee_subtracts_from_proceeds():
    policy = {"fee_status": "official", "fee_rate": Decimal("0.05")}
    calc = calculate_fee("sell", Decimal("10"), Decimal("0.30"), policy)
    assert calc["gross_notional"] == Decimal("3.00")
    assert calc["official_fee"] == Decimal("0.10500")
    assert calc["net_cost_or_proceeds"] == Decimal("2.89500")


def test_buy_and_sell_direction_are_not_mixed():
    policy = {"fee_status": "official", "fee_rate": Decimal("0.05")}
    buy = calculate_fee("buy", Decimal("10"), Decimal("0.30"), policy)
    sell = calculate_fee("sell", Decimal("10"), Decimal("0.30"), policy)
    assert buy["net_cost_or_proceeds"] - buy["gross_notional"] == Decimal("0.10500")
    assert sell["gross_notional"] - sell["net_cost_or_proceeds"] == Decimal("0.10500")


def test_partial_fill_fee_uses_actual_filled_shares():
    policy = {"fee_status": "official", "fee_rate": Decimal("0.05")}
    calc = calculate_fee("sell", Decimal("4"), Decimal("0.30"), policy)
    assert calc["official_fee"] == Decimal("0.04200")


def test_fee_crosscheck_gamma_clob_match():
    policy = extract_fee_policy(market(), clob())
    assert policy["fee_crosscheck_status"] == "official"
    assert policy["fee_rate"] == Decimal("0.05")


def test_fee_crosscheck_conflict_blocks_official():
    policy = extract_fee_policy(market(feeSchedule={"rate": "0.04"}), clob())
    assert policy["fee_crosscheck_status"] == "conflict"
    assert policy["fee_rate"] is None


def test_fee_clob_present_gamma_missing_is_official_with_note():
    policy = extract_fee_policy(market(feeSchedule={}), clob())
    assert policy["fee_crosscheck_status"] == "official"
    assert "gamma" in policy["fee_conflict_details"]


def test_fee_both_missing_unknown_not_zero():
    policy = extract_fee_policy(market(feeSchedule={}), {"condition_id": "0xdemo", "t": clob()["t"], "fd": {}})
    assert policy["fee_crosscheck_status"] == "unknown"
    calc = calculate_fee("buy", Decimal("10"), Decimal("0.30"), policy)
    assert calc["official_fee"] is None


def test_fee_disabled_zero():
    policy = extract_fee_policy(market(feesEnabled=False), {"condition_id": "0xdemo", "t": clob()["t"], "fd": {}})
    assert policy["fee_crosscheck_status"] == "disabled"
    assert calculate_fee("buy", Decimal("10"), Decimal("0.30"), policy)["official_fee"] == Decimal("0")


def test_gamma_clob_swapped_yes_no_detected():
    bad_clob = clob(t=[{"t": "no-token", "o": "Yes"}, {"t": "yes-token", "o": "No"}])
    validation = validate_token_mapping(signal_row(), market(), bad_clob, normalize_orderbook(book(), "yes-token", "0xdemo"))
    assert not validation["mapping_valid"]
    assert "clob_outcome_token_mismatch" in validation["errors"]


def test_orderbook_asset_mismatch_rejected():
    with pytest.raises(AdapterError):
        normalize_orderbook(book(asset_id="wrong-token"), "yes-token", "0xdemo")


def test_orderbook_condition_mismatch_rejected():
    with pytest.raises(AdapterError):
        normalize_orderbook(book(market="0xother"), "yes-token", "0xdemo")


def test_temperature_bucket_mismatch_rejected():
    validation = validate_token_mapping(signal_row(temperature_bucket="31C"), market(), clob(), normalize_orderbook(book(), "yes-token", "0xdemo"))
    assert not validation["mapping_valid"]
    assert "temperature_bucket_mismatch" in validation["errors"]


def test_tick_size_legal_and_illegal():
    normalize_orderbook(book(asks=[{"price": "0.301", "size": "10"}]), "yes-token", "0xdemo")
    with pytest.raises(AdapterError):
        normalize_orderbook(book(asks=[{"price": "0.3005", "size": "10"}]), "yes-token", "0xdemo")


def test_min_order_size_enforced_buy_and_sell():
    norm = normalize_orderbook(book(asks=[{"price": "0.30", "size": "4.999"}], bids=[{"price": "0.30", "size": "4.999"}]), "yes-token", "0xdemo")
    assert consume_buy_depth(norm, Decimal("2"), Decimal("0.31"))["status"] == "below_min_order_size"
    assert consume_sell_depth(norm, Decimal("4.999"))["status"] == "below_min_order_size"
    norm_ok = normalize_orderbook(book(bids=[{"price": "0.30", "size": "5"}]), "yes-token", "0xdemo")
    assert consume_sell_depth(norm_ok, Decimal("5"))["status"] in {"filled", "partial"}


def test_remaining_below_min_order_is_marked():
    norm = normalize_orderbook(book(bids=[{"price": "0.30", "size": "6"}]), "yes-token", "0xdemo")
    sell = consume_sell_depth(norm, Decimal("7"))
    assert sell["status"] == "partial"
    assert sell["remaining_below_min_order_size"] is True


def test_resolved_winning_asset_settlement():
    resolved = market(active=False, closed=True, resolved=True, winningAssetId="yes-token")
    evidence = parse_settlement_evidence(resolved, gamma_token_pairs(resolved))
    assert evidence["evidence_valid"]
    assert evidence["token_settlement_values"]["yes-token"] == "1"
    assert evidence["token_settlement_values"]["no-token"] == "0"


def test_resolved_winning_outcome_settlement():
    resolved = market(active=False, closed=True, resolved=True, winningOutcome="Yes")
    evidence = parse_settlement_evidence(resolved, gamma_token_pairs(resolved))
    assert evidence["evidence_valid"]
    assert evidence["winning_asset_id"] == "yes-token"


def test_resolved_missing_winner_unknown_not_default_zero():
    resolved = market(active=False, closed=True, resolved=True, outcomePrices=json.dumps(["0.50", "0.50"]))
    evidence = parse_settlement_evidence(resolved, gamma_token_pairs(resolved))
    assert not evidence["evidence_valid"]
    assert evidence["settlement_status"] == "unknown"
    assert evidence["token_settlement_values"]["yes-token"] is None


def test_outcome_prices_binary_can_resolve():
    resolved = market(active=False, closed=True, resolved=True, outcomePrices=json.dumps(["1", "0"]))
    evidence = parse_settlement_evidence(resolved, gamma_token_pairs(resolved))
    assert evidence["evidence_valid"]
    assert evidence["winning_asset_id"] == "yes-token"


def test_outcome_prices_conflict_rejected():
    resolved = market(active=False, closed=True, resolved=True, winningOutcome="Yes", outcomePrices=json.dumps(["0", "1"]))
    evidence = parse_settlement_evidence(resolved, gamma_token_pairs(resolved))
    assert evidence["settlement_status"] == "conflict"


def test_closed_unresolved_not_settleable():
    pending = market(active=False, closed=True, resolved=False)
    evidence = parse_settlement_evidence(pending, gamma_token_pairs(pending))
    assert not evidence["evidence_valid"]
    assert evidence["settlement_status"] == "not_settleable"


def test_duplicate_snapshot_same_run_idempotent(tmp_path):
    config = load_config(CONFIG)
    init_ledger(tmp_path, DEMO, CONFIG)
    signal_file = tmp_path / "sig.csv"
    write_signals(signal_file, [signal_row()])
    register_signals(tmp_path, DEMO, CONFIG, signal_file)
    conn = connect(db_path(tmp_path, DEMO, config))
    try:
        sig = conn.execute("SELECT * FROM signals").fetchone()
        sid1, _, inserted1 = record_snapshot(conn, "run1", DEMO, sig, "entry", book(), "fixture")
        sid2, _, inserted2 = record_snapshot(conn, "run1", DEMO, sig, "entry", book(), "fixture")
        sid3, _, inserted3 = record_snapshot(conn, "run2", DEMO, sig, "entry", book(), "fixture")
        assert sid1 == sid2
        assert inserted1 is True
        assert inserted2 is False
        assert sid3 != sid1
        assert inserted3 is True
    finally:
        conn.close()


def test_demo_run_integrity_and_strategy_fee_separation(tmp_path):
    result = demo_run(tmp_path, CONFIG)
    assert result["audit"]["ok"]
    config = load_config(CONFIG)
    conn = connect(db_path(tmp_path, DEMO, config))
    try:
        strategies = {r["strategy_id"] for r in conn.execute("SELECT DISTINCT strategy_id FROM strategy_lots")}
        assert strategies == set(STRATEGY_IDS)
        rows = conn.execute("SELECT * FROM event_results WHERE net_pnl IS NOT NULL").fetchall()
        assert rows
        for row in rows:
            gross = dec(row["gross_pnl"])
            fees = dec(row["total_fees"])
            net = dec(row["net_pnl"])
            assert gross - fees == net
    finally:
        conn.close()


def test_formal_status_empty_after_init(tmp_path):
    init_ledger(tmp_path, "formal", CONFIG)
    st = status(tmp_path, "formal", CONFIG)
    assert st["formal_started_at_utc"] == ""
    assert st["signals"] == 0
    assert st["snapshots"] == 0
    assert st["entry_fills"] == 0
    assert st["exit_fills"] == 0
    assert st["settlements"] == 0
    assert st["event_results"] == 0
