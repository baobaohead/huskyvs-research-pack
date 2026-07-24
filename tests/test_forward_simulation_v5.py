import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.forward_simulation_v5 import (
    DEMO_MODE,
    FORMAL_MODE,
    STRATEGY_IDS,
    audit,
    data_dir_for,
    demo,
    ensure_ledger,
    event_id_for,
    integrity_check,
    latest_position_states,
    process_entry,
    process_exits_for_signal,
    register_signals,
    settle_positions,
    simulate_buy_from_asks,
    simulate_sell_to_bids,
    normalize_orderbook,
    write_state,
)


def read_csv_rows(path: Path):
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def signal(signal_id="sig1", token_id="tok1", created_at=None):
    return {
        "signal_id": signal_id,
        "created_at_utc": created_at or datetime.now(timezone.utc).isoformat(),
        "city": "Demo City",
        "weather_date_local": "2099-01-01",
        "market_slug": "demo-weather-market",
        "condition_id": "cond1",
        "token_id": token_id,
        "outcome": "YES",
        "side": "BUY",
        "forecast_temperature": "30",
        "forecast_probability": "0.62",
        "market_probability_at_signal": "0.11",
        "intended_usd": "100",
        "max_entry_price": "0.10",
        "source": "pytest",
        "notes": "",
    }


def write_signals(path: Path, rows):
    fields = [
        "signal_id",
        "created_at_utc",
        "city",
        "weather_date_local",
        "market_slug",
        "condition_id",
        "token_id",
        "outcome",
        "side",
        "forecast_temperature",
        "forecast_probability",
        "market_probability_at_signal",
        "intended_usd",
        "max_entry_price",
        "source",
        "notes",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fields)
        writer.writeheader()
        writer.writerows(rows)


def entry_book():
    return {
        "bids": [{"price": "0.09", "size": "100"}],
        "asks": [{"price": "0.10", "size": "1000"}],
    }


def test_v5_four_strategies_receive_identical_entry(tmp_path):
    data_dir = tmp_path / "formal"
    ensure_ledger(data_dir)
    sig = signal()
    process_entry(data_dir, sig, entry_book(), FORMAL_MODE, "fixture")

    rows = [r for r in read_csv_rows(data_dir / "strategy_positions.csv") if r["event_type"] == "entry_buy"]

    assert {r["strategy_id"] for r in rows} == set(STRATEGY_IDS)
    assert len({r["remaining_shares"] for r in rows}) == 1
    assert len({r["remaining_cost_basis"] for r in rows}) == 1


def test_v5_buy_vwap_uses_ask_depth_not_best_price_only():
    book = normalize_orderbook(
        {
            "bids": [{"price": "0.09", "size": "100"}],
            "asks": [{"price": "0.10", "size": "100"}, {"price": "0.12", "size": "1000"}],
        },
        "tok",
    )
    result = simulate_buy_from_asks(book, intended_usd=20, max_entry_price=0.12)

    assert result["filled_shares"] > 100
    assert result["vwap"] > 0.10


def test_v5_exit_uses_bid_depth_vwap_not_best_bid(tmp_path):
    data_dir = tmp_path / "formal"
    ensure_ledger(data_dir)
    sig = signal()
    process_entry(data_dir, sig, entry_book(), FORMAL_MODE, "fixture")
    raw_book = {
        "bids": [{"price": "0.30", "size": "1"}, {"price": "0.10", "size": "1000"}],
        "asks": [{"price": "0.31", "size": "100"}],
    }

    result = process_exits_for_signal(data_dir, sig, raw_book, FORMAL_MODE, "fixture")

    assert all(r["status"] == "not_triggered" for r in result)
    assert read_csv_rows(data_dir / "exit_fills.csv") == []


def test_v5_depth_shortage_only_partially_fills_exit(tmp_path):
    data_dir = tmp_path / "formal"
    ensure_ledger(data_dir)
    sig = signal()
    process_entry(data_dir, sig, entry_book(), FORMAL_MODE, "fixture")
    raw_book = {
        "bids": [{"price": "0.25", "size": "100"}],
        "asks": [{"price": "0.26", "size": "100"}],
    }

    process_exits_for_signal(data_dir, sig, raw_book, FORMAL_MODE, "fixture")
    exits = read_csv_rows(data_dir / "exit_fills.csv")

    assert exits
    assert any(r["complete_fill"] == "False" for r in exits)
    states = latest_position_states(data_dir)
    assert all(state.remaining_shares >= 0 for state in states.values())


def test_v5_restart_does_not_duplicate_same_snapshot(tmp_path):
    data_dir = tmp_path / "formal"
    ensure_ledger(data_dir)
    sig = signal()
    process_entry(data_dir, sig, entry_book(), FORMAL_MODE, "fixture")
    raw_book = {"bids": [{"price": "0.25", "size": "1000"}], "asks": [{"price": "0.26", "size": "100"}]}

    process_exits_for_signal(data_dir, sig, raw_book, FORMAL_MODE, "fixture")
    process_exits_for_signal(data_dir, sig, raw_book, FORMAL_MODE, "fixture")

    exits = read_csv_rows(data_dir / "exit_fills.csv")
    assert len(exits) == 2
    assert {r["strategy_id"] for r in exits} == {"tp_2x_sell_50pct", "tp_2x_sell_75pct"}


def test_v5_formal_rejects_historical_signal(tmp_path):
    root = tmp_path
    data_dir = data_dir_for(root, FORMAL_MODE)
    ensure_ledger(data_dir)
    started = datetime.now(timezone.utc)
    write_state(data_dir, {"formal_started_at_utc": started.isoformat()})
    old_signal = signal(created_at=(started - timedelta(seconds=1)).isoformat())
    input_file = tmp_path / "old.csv"
    write_signals(input_file, [old_signal])

    with pytest.raises(ValueError, match="historical signal"):
        register_signals(data_dir, input_file, FORMAL_MODE)


def test_v5_demo_data_is_separate_from_formal(tmp_path):
    demo(tmp_path)
    formal_dir = data_dir_for(tmp_path, FORMAL_MODE)
    ensure_ledger(formal_dir)

    assert read_csv_rows(formal_dir / "signals.csv") == []
    assert integrity_check(formal_dir, FORMAL_MODE)["demo_data_isolated"]


def test_v5_strategy_inventories_are_independent(tmp_path):
    data_dir = tmp_path / "formal"
    ensure_ledger(data_dir)
    sig = signal()
    process_entry(data_dir, sig, entry_book(), FORMAL_MODE, "fixture")
    raw_book = {"bids": [{"price": "0.25", "size": "1000"}], "asks": [{"price": "0.26", "size": "100"}]}
    process_exits_for_signal(data_dir, sig, raw_book, FORMAL_MODE, "fixture")
    states = latest_position_states(data_dir)

    assert states[("hold_to_settlement", "tok1")].remaining_shares == 1000
    assert states[("tp_2x_sell_50pct", "tok1")].remaining_shares == 500
    assert states[("tp_2x_sell_75pct", "tok1")].remaining_shares == 250
    assert states[("tp_5x_sell_25pct", "tok1")].remaining_shares == 1000


def test_v5_rolls_add_on_signal_into_token_level_cost(tmp_path):
    data_dir = tmp_path / "formal"
    ensure_ledger(data_dir)
    sig1 = signal("sig1", "tok1")
    sig2 = signal("sig2", "tok1")
    sig2["intended_usd"] = "100"
    sig2["max_entry_price"] = "0.20"
    process_entry(data_dir, sig1, entry_book(), FORMAL_MODE, "fixture")
    process_entry(
        data_dir,
        sig2,
        {"bids": [{"price": "0.18", "size": "100"}], "asks": [{"price": "0.20", "size": "1000"}]},
        FORMAL_MODE,
        "fixture",
    )
    states = latest_position_states(data_dir)

    assert states[("hold_to_settlement", "tok1")].remaining_shares == 1500
    assert round(states[("hold_to_settlement", "tok1")].rolling_avg_cost, 6) == round(200 / 1500, 6)


def test_v5_settlement_pnl_is_correct(tmp_path):
    data_dir = tmp_path / "formal"
    ensure_ledger(data_dir)
    sig = signal()
    process_entry(data_dir, sig, entry_book(), FORMAL_MODE, "fixture")
    settlement_file = tmp_path / "settle.csv"
    with settlement_file.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, ["signal_id", "token_id", "settlement_price", "settled_at_utc", "notes"])
        writer.writeheader()
        writer.writerow({"signal_id": "sig1", "token_id": "tok1", "settlement_price": "1", "settled_at_utc": datetime.now(timezone.utc).isoformat(), "notes": ""})

    rows = settle_positions(data_dir, settlement_file, FORMAL_MODE)

    assert len(rows) == 4
    assert all(float(r["gross_pnl"]) == 900 for r in rows)


def test_v5_event_id_aggregates_same_city_date_market():
    sig1 = signal("sig1", "token-30c")
    sig2 = signal("sig2", "token-31c")
    sig2["outcome"] = "NO"

    assert event_id_for(sig1) == event_id_for(sig2)


def test_v5_audit_log_is_append_only(tmp_path):
    data_dir = tmp_path / "formal"
    ensure_ledger(data_dir)
    audit(data_dir, "one", {"a": 1})
    first = (data_dir / "audit_log.jsonl").read_text(encoding="utf-8")
    audit(data_dir, "two", {"b": 2})
    second = (data_dir / "audit_log.jsonl").read_text(encoding="utf-8")

    assert second.startswith(first)
    assert len(second.splitlines()) == len(first.splitlines()) + 1
