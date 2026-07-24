import csv
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.forward_simulation_v5_1 import (
    DEMO,
    FORMAL,
    STRATEGY_IDS,
    LedgerLock,
    aggregate_results,
    assert_formal_hashes,
    audit,
    audit_integrity,
    data_dir,
    ensure_ledger,
    latest_entry_state,
    load_config,
    make_event_key,
    process_entry,
    process_exit,
    register_signals,
    run_loop,
    settle,
    signal_position,
    start_formal,
)


def config():
    return {
        "entry": {"entry_valid_minutes": 10},
        "sample_rules": {"max_signal_registration_delay_seconds": 300, "allowed_future_skew_seconds": 30},
        "fees": {"entry_fee_bps": 10, "exit_fee_bps": 10, "settlement_fee_bps": 0},
        "polling": {"default_interval_seconds": 60},
    }


def base_signal(signal_id="sig1", token_id="tok1", created=None, intended="100", max_price="0.10", city="City", date="2099-01-01", metric="high"):
    created = created or datetime.now(timezone.utc)
    return {
        "signal_id": signal_id,
        "created_at_utc": created.isoformat(),
        "city": city,
        "weather_date_local": date,
        "weather_metric": metric,
        "market_slug": "weather-market",
        "condition_id": "cond",
        "token_id": token_id,
        "outcome": "YES",
        "side": "BUY",
        "forecast_temperature": "30",
        "forecast_probability": "0.6",
        "market_probability_at_signal": "0.1",
        "intended_usd": intended,
        "max_entry_price": max_price,
        "source": "pytest",
        "notes": "",
        "registered_at_utc": created.isoformat(),
        "city_normalized": "city",
        "event_key": make_event_key(city, date, metric),
        "entry_deadline_utc": (created + timedelta(minutes=10)).isoformat(),
        "mode": DEMO,
    }


def write_signals(path: Path, rows):
    fields = ["signal_id", "created_at_utc", "city", "weather_date_local", "weather_metric", "market_slug", "condition_id", "token_id", "outcome", "side", "forecast_temperature", "forecast_probability", "market_probability_at_signal", "intended_usd", "max_entry_price", "source", "notes"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def read_rows(path: Path):
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def entry_book(ts="entry", price="0.10", size="1000"):
    return {"timestamp": ts, "bids": [{"price": "0.09", "size": "100"}], "asks": [{"price": price, "size": size}]}


def exit_book(ts="exit", price="0.25", size="1000"):
    return {"timestamp": ts, "bids": [{"price": price, "size": size}], "asks": [{"price": "0.26", "size": "100"}]}


def funded_signal(tmp_path, sig=None):
    path = tmp_path / "demo"
    ensure_ledger(path)
    sig = sig or base_signal()
    sig_file = tmp_path / f"{sig['signal_id']}.csv"
    write_signals(sig_file, [sig])
    registered = register_signals(path, sig_file, DEMO, config(), tmp_path, tmp_path / "cfg")
    sig = registered[0]
    process_entry(path, sig, entry_book(), DEMO, config())
    return path, sig


def test_v5_1_50pct_trigger_only_once_across_three_high_snapshots(tmp_path):
    path, sig = funded_signal(tmp_path)
    for i in range(3):
        process_exit(path, sig, exit_book(f"exit-{i}"), DEMO, config())

    assert signal_position(path, "tp_2x_sell_50pct", sig["signal_id"])["shares"] == pytest.approx(500)


def test_v5_1_75pct_trigger_only_once_across_three_high_snapshots(tmp_path):
    path, sig = funded_signal(tmp_path)
    for i in range(3):
        process_exit(path, sig, exit_book(f"exit-{i}"), DEMO, config())

    assert signal_position(path, "tp_2x_sell_75pct", sig["signal_id"])["shares"] == pytest.approx(250)


def test_v5_1_partial_take_profit_continues_to_fixed_target(tmp_path):
    path, sig = funded_signal(tmp_path)
    process_exit(path, sig, exit_book("exit-1", size="200"), DEMO, config())
    process_exit(path, sig, exit_book("exit-2", size="300"), DEMO, config())

    assert signal_position(path, "tp_2x_sell_50pct", sig["signal_id"])["shares"] == pytest.approx(500)
    trigger = [r for r in read_rows(path / "strategy_triggers.csv") if r["strategy_id"] == "tp_2x_sell_50pct"][-1]
    assert float(trigger["trigger_filled_shares"]) == pytest.approx(500)
    assert trigger["trigger_status"] == "completed"
    row = [r for r in aggregate_results(path) if r["strategy_id"] == "tp_2x_sell_50pct"][0]
    assert row["incomplete_take_profit"] is False


def test_v5_1_price_drop_then_rise_does_not_retrigger(tmp_path):
    path, sig = funded_signal(tmp_path)
    process_exit(path, sig, exit_book("exit-1"), DEMO, config())
    process_exit(path, sig, exit_book("exit-low", price="0.11"), DEMO, config())
    process_exit(path, sig, exit_book("exit-rise"), DEMO, config())

    assert signal_position(path, "tp_2x_sell_50pct", sig["signal_id"])["shares"] == pytest.approx(500)


def test_v5_1_event_key_groups_adjacent_temperature_bins():
    assert make_event_key("New York City", "2099-01-01", "high") == make_event_key(" new   york city ", "2099-01-01", "highest temperature")


def test_v5_1_event_key_splits_date_and_metric():
    assert make_event_key("City", "2099-01-01", "high") != make_event_key("City", "2099-01-02", "high")
    assert make_event_key("City", "2099-01-01", "high") != make_event_key("City", "2099-01-01", "low")


def test_v5_1_unfilled_and_unsettled_do_not_count_as_settled_events(tmp_path):
    path = tmp_path / "demo"
    ensure_ledger(path)
    sig_file = tmp_path / "signals.csv"
    write_signals(sig_file, [base_signal("sig1"), base_signal("sig2", token_id="tok2")])
    register_signals(path, sig_file, DEMO, config(), tmp_path, tmp_path / "cfg.yaml")
    process_entry(path, base_signal("sig1"), entry_book(), DEMO, config())
    rows = aggregate_results(path)
    hold = [r for r in rows if r["strategy_id"] == "hold_to_settlement" and r["event_key"] == make_event_key("City", "2099-01-01", "high")][0]

    assert hold["traded_event_count"] == 1
    assert hold["settled_event_count"] == 0


def test_v5_1_partial_entry_can_fill_remaining_later(tmp_path):
    path = tmp_path / "demo"
    ensure_ledger(path)
    sig = base_signal()
    process_entry(path, sig, entry_book("entry-1", size="100"), DEMO, config())
    process_entry(path, sig, entry_book("entry-2", size="900"), DEMO, config())
    state = latest_entry_state(path)[sig["signal_id"]]

    assert float(state["filled_entry_usd"]) == pytest.approx(100)
    assert state["entry_status"] == "filled"
    for strategy_id in STRATEGY_IDS:
        assert signal_position(path, strategy_id, sig["signal_id"])["shares"] == pytest.approx(1000)


def test_v5_1_duplicate_entry_snapshot_does_not_refill(tmp_path):
    path = tmp_path / "demo"
    ensure_ledger(path)
    sig = base_signal()
    raw = entry_book("same", size="100")
    process_entry(path, sig, raw, DEMO, config())
    process_entry(path, sig, raw, DEMO, config())

    assert float(latest_entry_state(path)[sig["signal_id"]]["filled_entry_usd"]) == pytest.approx(10)


def test_v5_1_entry_expires_and_high_ask_does_not_fill(tmp_path):
    path = tmp_path / "demo"
    ensure_ledger(path)
    created = datetime.now(timezone.utc)
    sig = base_signal(created=created)
    process_entry(path, sig, entry_book("high", price="0.11"), DEMO, config(), now=created + timedelta(minutes=1))
    assert float(latest_entry_state(path)[sig["signal_id"]]["filled_entry_usd"]) == 0
    process_entry(path, sig, entry_book("late"), DEMO, config(), now=created + timedelta(minutes=11))
    assert latest_entry_state(path)[sig["signal_id"]]["entry_status"] == "expired"


def test_v5_1_same_token_two_signals_settle_with_separate_pnl(tmp_path):
    path = tmp_path / "demo"
    ensure_ledger(path)
    sig1 = base_signal("sig1", "tok")
    sig2 = base_signal("sig2", "tok", intended="50", max_price="0.20")
    sig_file = tmp_path / "signals.csv"
    write_signals(sig_file, [sig1, sig2])
    sig1, sig2 = register_signals(path, sig_file, DEMO, config(), tmp_path, tmp_path / "cfg")
    process_entry(path, sig1, entry_book("entry-1"), DEMO, config())
    process_entry(path, sig2, entry_book("entry-2", price="0.20", size="250"), DEMO, config())
    set_file = tmp_path / "settle.csv"
    write_settlements(set_file, [("sig1", "tok", "1"), ("sig2", "tok", "1")])
    settle(path, set_file, DEMO, config())
    rows = read_rows(path / "settlements.csv")
    sig1_hold = [r for r in rows if r["signal_id"] == "sig1" and r["strategy_id"] == "hold_to_settlement"][0]
    sig2_hold = [r for r in rows if r["signal_id"] == "sig2" and r["strategy_id"] == "hold_to_settlement"][0]

    assert float(sig1_hold["settlement_proceeds"]) == pytest.approx(1000)
    assert float(sig2_hold["settlement_proceeds"]) == pytest.approx(250)


def test_v5_1_one_signal_exit_does_not_pollute_other_same_token(tmp_path):
    path = tmp_path / "demo"
    ensure_ledger(path)
    sig1 = base_signal("sig1", "tok")
    sig2 = base_signal("sig2", "tok")
    sig_file = tmp_path / "signals.csv"
    write_signals(sig_file, [sig1, sig2])
    sig1, sig2 = register_signals(path, sig_file, DEMO, config(), tmp_path, tmp_path / "cfg")
    process_entry(path, sig1, entry_book("entry-1"), DEMO, config())
    process_entry(path, sig2, entry_book("entry-2"), DEMO, config())
    process_exit(path, sig1, exit_book("exit"), DEMO, config())

    assert signal_position(path, "tp_2x_sell_50pct", "sig1")["shares"] == pytest.approx(500)
    assert signal_position(path, "tp_2x_sell_50pct", "sig2")["shares"] == pytest.approx(1000)


def test_v5_1_fifo_allocation_for_partial_sell(tmp_path):
    path = tmp_path / "demo"
    ensure_ledger(path)
    sig = base_signal(intended="200", max_price="0.20")
    sig_file = tmp_path / "signals.csv"
    write_signals(sig_file, [sig])
    sig = register_signals(path, sig_file, DEMO, config(), tmp_path, tmp_path / "cfg")[0]
    process_entry(path, sig, entry_book("entry-1", price="0.10", size="1000"), DEMO, config())
    process_entry(path, sig, entry_book("entry-2", price="0.20", size="500"), DEMO, config())
    process_exit(path, sig, exit_book("exit", price="0.35", size="750"), DEMO, config())
    allocs = [r for r in read_rows(path / "exit_fill_allocations.csv") if r["strategy_id"] == "tp_2x_sell_50pct"]

    assert len(allocs) == 1
    assert float(allocs[0]["allocated_shares"]) == pytest.approx(750)


def prepare_formal_root(tmp_path):
    root = tmp_path
    (root / "src").mkdir()
    (root / "reports").mkdir()
    cfg = root / "config.yaml"
    cfg.write_text("entry:\n  entry_valid_minutes: 10\nsample_rules:\n  max_signal_registration_delay_seconds: 300\n  allowed_future_skew_seconds: 30\nfees:\n  entry_fee_bps: 10\n  exit_fee_bps: 10\n  settlement_fee_bps: 0\n", encoding="utf-8")
    (root / "src/forward_simulation_v5_1.py").write_text("core", encoding="utf-8")
    (root / "src/forward_reporting_v5_1.py").write_text("report", encoding="utf-8")
    (root / "reports/FORWARD_SIMULATION_V5_1_PREREGISTRATION.md").write_text("pre", encoding="utf-8")
    path = data_dir(root, FORMAL)
    ensure_ledger(path)
    start_formal(root, cfg, True)
    return root, cfg, path


def test_v5_1_hash_freeze_rejects_config_and_code_drift(tmp_path):
    root, cfg, path = prepare_formal_root(tmp_path)
    cfg.write_text(cfg.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="hash freeze mismatch"):
        assert_formal_hashes(path, root, cfg, FORMAL)
    cfg.write_text("entry:\n  entry_valid_minutes: 10\nsample_rules:\n  max_signal_registration_delay_seconds: 300\n  allowed_future_skew_seconds: 30\nfees:\n  entry_fee_bps: 10\n  exit_fee_bps: 10\n  settlement_fee_bps: 0\n", encoding="utf-8")
    start_formal(root, cfg, True)
    (root / "src/forward_simulation_v5_1.py").write_text("core drift", encoding="utf-8")
    with pytest.raises(RuntimeError, match="hash freeze mismatch"):
        assert_formal_hashes(path, root, cfg, FORMAL)
    with pytest.raises(RuntimeError, match="hash freeze mismatch"):
        process_entry(path, base_signal("blocked_write"), entry_book(), FORMAL, load_config(cfg), root=root, config_path=cfg)


def test_v5_1_signal_freshness_rules(tmp_path):
    root, cfg, path = prepare_formal_root(tmp_path)
    conf = load_config(cfg)
    now = datetime.now(timezone.utc) + timedelta(seconds=301)
    sig_file = tmp_path / "fresh.csv"
    write_signals(sig_file, [base_signal("fresh", created=now - timedelta(seconds=299))])
    assert len(register_signals(path, sig_file, FORMAL, conf, root, cfg, now)) == 1
    stale_file = tmp_path / "stale.csv"
    write_signals(stale_file, [base_signal("stale", created=now - timedelta(seconds=301)), base_signal("future", created=now + timedelta(seconds=31)), base_signal("before", created=now - timedelta(days=1))])
    register_signals(path, stale_file, FORMAL, conf, root, cfg, now)
    signals = {r["signal_id"] for r in read_rows(path / "signals.csv")}
    assert "stale" not in signals and "future" not in signals and "before" not in signals


def test_v5_1_fees_enter_net_pnl(tmp_path):
    path, sig = funded_signal(tmp_path)
    process_exit(path, sig, exit_book(), DEMO, config())
    set_file = tmp_path / "settle.csv"
    write_settlements(set_file, [(sig["signal_id"], sig["token_id"], "0")])
    settle(path, set_file, DEMO, config())
    rows = aggregate_results(path)
    row = [r for r in rows if r["strategy_id"] == "tp_2x_sell_50pct"][0]

    assert row["total_fees"] > 0
    assert row["gross_pnl"] - row["net_pnl"] == pytest.approx(row["total_fees"])


def test_v5_1_run_loop_recovers_after_market_error(tmp_path):
    path = tmp_path / "demo"
    ensure_ledger(path)
    sig = base_signal()
    sig_file = tmp_path / "signals.csv"
    write_signals(sig_file, [sig])
    register_signals(path, sig_file, DEMO, config(), tmp_path, tmp_path / "cfg")
    calls = {"n": 0}

    def provider(token, purpose):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("network down")
        return entry_book(f"entry-{calls['n']}")

    run_loop(path, DEMO, config(), provider, iterations=2)
    assert float(latest_entry_state(path)[sig["signal_id"]]["filled_entry_usd"]) == pytest.approx(100)


def test_v5_1_pause_resume_and_single_instance_lock(tmp_path):
    path = tmp_path / "demo"
    ensure_ledger(path)
    with LedgerLock(path):
        with pytest.raises(RuntimeError, match="already holds"):
            with LedgerLock(path):
                pass
    state = path / "system_state.json"
    data = state.read_text(encoding="utf-8")
    assert "paused" in data


def test_v5_1_settlement_conflict_rejected_and_after_settlement_no_exit(tmp_path):
    path, sig = funded_signal(tmp_path)
    set_file = tmp_path / "settle.csv"
    write_settlements(set_file, [(sig["signal_id"], sig["token_id"], "0")])
    settle(path, set_file, DEMO, config())
    with pytest.raises(RuntimeError, match="conflicting settlement"):
        conflict = tmp_path / "conflict.csv"
        write_settlements(conflict, [(sig["signal_id"], sig["token_id"], "1")])
        settle(path, conflict, DEMO, config())
    process_exit(path, sig, exit_book("after-settle"), DEMO, config())
    assert read_rows(path / "exit_fills.csv") == []


def test_v5_1_audit_integrity_passes_and_audit_log_append_only(tmp_path):
    path, sig = funded_signal(tmp_path)
    before = (path / "audit_log.jsonl").read_text(encoding="utf-8")
    audit(path, "extra", {"ok": True})
    after = (path / "audit_log.jsonl").read_text(encoding="utf-8")
    assert after.startswith(before)
    assert audit_integrity(path, tmp_path, tmp_path / "cfg", DEMO)["ok"]


def write_settlements(path: Path, rows):
    fields = ["signal_id", "condition_id", "token_id", "settlement_outcome", "settlement_value", "source", "source_reference", "observed_at_utc", "operator_notes"]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fields)
        writer.writeheader()
        for signal_id, token_id, value in rows:
            writer.writerow({"signal_id": signal_id, "condition_id": "cond", "token_id": token_id, "settlement_outcome": "YES" if value == "1" else "NO", "settlement_value": value, "source": "fixture", "source_reference": "fixture://settlement", "observed_at_utc": datetime.now(timezone.utc).isoformat(), "operator_notes": ""})
