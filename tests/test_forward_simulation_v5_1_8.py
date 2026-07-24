import csv
import json
import shutil
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import src.forward_simulation_v5_1_8 as sim  # noqa: E402

from src.forward_simulation_v5_1_8 import (  # noqa: E402
    DEMO,
    FixtureAdapter,
    STRATEGY_IDS,
    audit_integrity,
    connect,
    data_dir,
    db_path,
    demo_fixture,
    demo_run,
    init_ledger,
    load_config,
    lock_path,
    lock_recovery_decision,
    monitor_once,
    monitor_control,
    parse_utc,
    record_snapshot,
    register_signals,
    run_loop,
    start_formal,
    status,
)
from src.polymarket_public_adapter_v5_1_8 import (  # noqa: E402
    AdapterError,
    PublicAdapter,
    calculate_fee,
    clob_token_pairs,
    consume_buy_depth,
    consume_sell_depth,
    content_hash,
    dec,
    extract_fee_policy,
    gamma_token_pairs,
    market_state,
    normalize_orderbook,
    parse_settlement_evidence,
    parse_temperature_bucket,
    parse_weather_market,
    validate_token_mapping,
)


CONFIG = PROJECT_ROOT / "config/forward_simulation_v5_1_8.yaml"


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
    assert "TOKEN_ID_MISMATCH" in validation["errors"]


def test_orderbook_asset_mismatch_rejected():
    with pytest.raises(AdapterError):
        normalize_orderbook(book(asset_id="wrong-token"), "yes-token", "0xdemo")


def test_orderbook_condition_mismatch_rejected():
    with pytest.raises(AdapterError):
        normalize_orderbook(book(market="0xother"), "yes-token", "0xdemo")


def test_temperature_bucket_mismatch_rejected():
    validation = validate_token_mapping(signal_row(temperature_bucket="31C"), market(), clob(), normalize_orderbook(book(), "yes-token", "0xdemo"))
    assert not validation["mapping_valid"]
    assert "TEMPERATURE_THRESHOLD_MISMATCH" in validation["errors"]


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
    resolved = market(active=False, closed=True, resolved=True, winningOutcome="Yes", umaResolutionStatus="final")
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
    assert not evidence["evidence_valid"]
    assert evidence["settlement_status"] == "unknown"


def test_outcome_prices_conflict_rejected():
    resolved = market(active=False, closed=True, resolved=True, winningOutcome="Yes", outcomePrices=json.dumps(["0", "1"]))
    evidence = parse_settlement_evidence(resolved, gamma_token_pairs(resolved))
    assert evidence["settlement_status"] == "conflict"


def test_closed_unresolved_not_settleable():
    pending = market(active=False, closed=True, resolved=False)
    evidence = parse_settlement_evidence(pending, gamma_token_pairs(pending))
    assert not evidence["evidence_valid"]
    assert evidence["settlement_status"] == "closed_unresolved"


def test_duplicate_snapshot_same_run_idempotent(tmp_path):
    config = load_config(CONFIG)
    init_ledger(tmp_path, DEMO, CONFIG)
    signal_file = tmp_path / "sig.csv"
    write_signals(signal_file, [signal_row()])
    register_signals(tmp_path, DEMO, CONFIG, signal_file, now=parse_utc("2099-01-01T00:00:01+00:00"))
    conn = connect(db_path(tmp_path, DEMO, config))
    try:
        conn.execute("INSERT INTO runs(run_id,mode,command,started_at_utc) VALUES('run1',?,'pytest','2099-01-01T00:00:02+00:00')", (DEMO,))
        conn.execute("INSERT INTO runs(run_id,mode,command,started_at_utc) VALUES('run2',?,'pytest','2099-01-01T00:00:03+00:00')", (DEMO,))
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


def test_market_state_resolved_blocks_entry_exit_and_only_settles(tmp_path):
    config = load_config(CONFIG)
    init_ledger(tmp_path, DEMO, CONFIG)
    signal_file = tmp_path / "sig.csv"
    write_signals(signal_file, [signal_row()])
    now = parse_utc("2099-01-01T00:00:05+00:00")
    register_signals(tmp_path, DEMO, CONFIG, signal_file, now=now)
    unresolved_adapter = FixtureAdapter(market(active=False, closed=True, resolved=False), clob(), [book()])
    unresolved = monitor_once(tmp_path, DEMO, CONFIG, run_id="closed_unresolved", adapter=unresolved_adapter, now=now)
    assert unresolved["results"][0]["entry_exit_blocked"] is True
    conn = connect(db_path(tmp_path, DEMO, config))
    try:
        assert conn.execute("SELECT COUNT(*) c FROM entry_fills").fetchone()["c"] == 0
    finally:
        conn.close()

    active_adapter = FixtureAdapter(market(), clob(), [book(asks=[{"price": "0.10", "size": "1000"}], bids=[{"price": "0.09", "size": "1000"}])])
    monitor_once(tmp_path, DEMO, CONFIG, run_id="active_entry", adapter=active_adapter, now=now)
    resolved_market = market(active=False, closed=True, resolved=True, winningOutcome="Yes", outcomePrices=json.dumps(["1", "0"]), umaResolutionStatus="final")
    resolved_adapter = FixtureAdapter(resolved_market, clob(), [book(bids=[{"price": "0.90", "size": "1000"}])])
    settled = monitor_once(tmp_path, DEMO, CONFIG, run_id="resolved_only", adapter=resolved_adapter, now=parse_utc("2099-01-03T00:00:00+00:00"))
    assert any(r["status"] == "settled" for r in settled["results"])
    conn = connect(db_path(tmp_path, DEMO, config))
    try:
        assert conn.execute("SELECT COUNT(*) c FROM exit_fills").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM settlements").fetchone()["c"] == len(STRATEGY_IDS)
    finally:
        conn.close()


def test_shared_entry_depth_100_shares_two_signals_total_fill_is_100(tmp_path):
    config = load_config(CONFIG)
    init_ledger(tmp_path, DEMO, CONFIG)
    rows = [
        signal_row(signal_id="sig-a", intended_usd="10", max_entry_price="0.11"),
        signal_row(signal_id="sig-b", intended_usd="10", max_entry_price="0.11"),
    ]
    signal_file = tmp_path / "signals.csv"
    write_signals(signal_file, rows)
    now = parse_utc("2099-01-01T00:00:02+00:00")
    register_signals(tmp_path, DEMO, CONFIG, signal_file, now=now)
    adapter = FixtureAdapter(market(), clob(), [book(asks=[{"price": "0.10", "size": "100"}], bids=[{"price": "0.09", "size": "1000"}])])
    monitor_once(tmp_path, DEMO, CONFIG, run_id="shared_entry", adapter=adapter, now=now)
    conn = connect(db_path(tmp_path, DEMO, config))
    try:
        total = dec(conn.execute("SELECT COALESCE(SUM(CAST(filled_shares AS REAL)),0) v FROM entry_fills").fetchone()["v"])
        assert total == Decimal("100")
        assert conn.execute("SELECT COUNT(*) c FROM orderbook_snapshots").fetchone()["c"] == 1
        assert adapter.calls == 1
        assert audit_integrity(tmp_path, DEMO, CONFIG)["checks"]["entry_shared_depth_overfill"] == 0
    finally:
        conn.close()


def test_same_strategy_exits_share_bid_depth_but_strategy_branches_are_independent(tmp_path):
    config = load_config(CONFIG)
    init_ledger(tmp_path, DEMO, CONFIG)
    rows = [
        signal_row(signal_id="sig-a", intended_usd="10", max_entry_price="0.11"),
        signal_row(signal_id="sig-b", intended_usd="10", max_entry_price="0.11"),
    ]
    signal_file = tmp_path / "signals.csv"
    write_signals(signal_file, rows)
    now = parse_utc("2099-01-01T00:00:02+00:00")
    register_signals(tmp_path, DEMO, CONFIG, signal_file, now=now)
    entry_adapter = FixtureAdapter(market(), clob(), [book(asks=[{"price": "0.10", "size": "200"}], bids=[{"price": "0.09", "size": "1000"}], hash="entry")])
    monitor_once(tmp_path, DEMO, CONFIG, run_id="entry_two", adapter=entry_adapter, now=now)
    exit_adapter = FixtureAdapter(market(), clob(), [book(asks=[{"price": "0.26", "size": "1000"}], bids=[{"price": "0.25", "size": "50"}], hash="exit")])
    monitor_once(tmp_path, DEMO, CONFIG, run_id="exit_shared", adapter=exit_adapter, now=parse_utc("2099-01-01T00:00:03+00:00"))
    conn = connect(db_path(tmp_path, DEMO, config))
    try:
        by_strategy = {
            r["strategy_id"]: dec(r["v"])
            for r in conn.execute("SELECT strategy_id,COALESCE(SUM(CAST(filled_shares AS REAL)),0) v FROM exit_fills GROUP BY strategy_id")
        }
        assert by_strategy["tp_2x_sell_50pct"] == Decimal("50")
        assert by_strategy["tp_2x_sell_75pct"] == Decimal("50")
        assert by_strategy.get("tp_5x_sell_25pct", Decimal("0")) == Decimal("0")
        assert audit_integrity(tmp_path, DEMO, CONFIG)["checks"]["exit_shared_depth_overfill"] == 0
    finally:
        conn.close()


def test_strict_future_and_system_registered_at_rejections(tmp_path):
    init_ledger(tmp_path, DEMO, CONFIG)
    base = parse_utc("2099-01-01T00:00:00+00:00")
    signal_file = tmp_path / "future.csv"
    rows = [
        signal_row(signal_id="ok-30s", created_at_utc="2099-01-01T00:00:30+00:00"),
        signal_row(signal_id="future-31s", created_at_utc="2099-01-01T00:00:31+00:00"),
        signal_row(signal_id="future-1h", created_at_utc="2099-01-01T01:00:00+00:00"),
        signal_row(signal_id="non-utc", created_at_utc="2099-01-01T00:00:00+08:00"),
        {**signal_row(signal_id="user-registered"), "registered_at_utc": "2099-01-01T00:00:00+00:00"},
    ]
    fields = list(signal_row().keys()) + ["registered_at_utc"]
    with signal_file.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    accepted = register_signals(tmp_path, DEMO, CONFIG, signal_file, now=base)
    assert [r["signal_id"] for r in accepted] == ["ok-30s"]


def copy_v514_release_files(dst: Path) -> Path:
    for rel in [
        "src/forward_simulation_v5_1_8.py",
        "src/forward_reporting_v5_1_8.py",
        "src/polymarket_public_adapter_v5_1_8.py",
        "schemas/forward_simulation_v5_1_8.sql",
        "config/forward_simulation_v5_1_8.yaml",
    ]:
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PROJECT_ROOT / rel, target)
    for rel, body in {
        "reports/FORWARD_SIMULATION_V5_1_8_PREREGISTRATION.md": "preregistration\n",
        "reports/FORWARD_SIMULATION_V5_1_8_API_CONTRACT.md": "api contract\n",
        "reports/FORWARD_SIMULATION_V5_1_8_FEE_CONTRACT.md": "fee contract\n",
        "reports/FORWARD_SIMULATION_V5_1_8_SETTLEMENT_FINALITY_CONTRACT.md": "settlement finality contract\n",
    }.items():
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return dst / "config/forward_simulation_v5_1_8.yaml"


def test_deleted_frozen_file_refuses_formal_write(tmp_path):
    cfg = copy_v514_release_files(tmp_path)
    start_formal(tmp_path, cfg, confirm=True, now=parse_utc("2099-01-01T00:00:00+00:00"))
    (tmp_path / "reports/FORWARD_SIMULATION_V5_1_8_API_CONTRACT.md").unlink()
    signal_file = tmp_path / "formal.csv"
    write_signals(signal_file, [signal_row(created_at_utc="2099-01-01T00:00:01+00:00")])
    with pytest.raises(RuntimeError):
        register_signals(tmp_path, "formal", cfg, signal_file, now=parse_utc("2099-01-01T00:00:02+00:00"))


def test_run_loop_lock_heartbeat_pause_resume_stop(tmp_path):
    config = load_config(CONFIG)
    init_ledger(tmp_path, DEMO, CONFIG)
    with pytest.raises(RuntimeError):
        run_loop(tmp_path, DEMO, CONFIG, iterations=0, interval_seconds=Decimal("0"))
    result = run_loop(tmp_path, DEMO, CONFIG, iterations=1, interval_seconds=Decimal("0"), run_id="loop_once")
    assert result["iterations_completed"] == 1
    assert not lock_path(tmp_path, DEMO, config).exists()
    st = status(tmp_path, DEMO, CONFIG)
    assert st["heartbeat"]["run_id"] == "loop_once"
    assert monitor_control(tmp_path, DEMO, CONFIG, "pause")["paused"] == "true"
    paused = monitor_once(tmp_path, DEMO, CONFIG, run_id="paused_once")
    assert paused["paused"] is True
    assert monitor_control(tmp_path, DEMO, CONFIG, "resume")["paused"] == "false"
    assert monitor_control(tmp_path, DEMO, CONFIG, "stop")["stopped"] == "true"


def test_fee_disabled_conflicts_and_unsupported_exponent():
    disabled_zero = extract_fee_policy(market(feesEnabled=False, feeSchedule={"rate": "0", "exponent": "1"}), clob(fd={"r": "0", "e": "1", "disabled": True}))
    assert disabled_zero["fee_crosscheck_status"] == "disabled"
    gamma_disabled_clob_nonzero = extract_fee_policy(market(feesEnabled=False), clob(fd={"r": "0.01", "e": "1"}))
    assert gamma_disabled_clob_nonzero["fee_crosscheck_status"] == "conflict"
    clob_disabled_gamma_nonzero = extract_fee_policy(market(feesEnabled=True, feeSchedule={"rate": "0.01", "exponent": "1"}), clob(fd={"r": "0", "e": "1", "disabled": True}))
    assert clob_disabled_gamma_nonzero["fee_crosscheck_status"] == "conflict"
    unsupported = extract_fee_policy(market(), clob(fd={"r": "0.05", "e": "2"}))
    assert unsupported["fee_crosscheck_status"] == "unsupported_fee_exponent"
    assert calculate_fee("buy", Decimal("10"), Decimal("0.30"), unsupported)["net_cost_or_proceeds"] is None


def test_tick_and_min_missing_or_gamma_conflict_rejects():
    with pytest.raises(AdapterError):
        normalize_orderbook({k: v for k, v in book().items() if k != "tick_size"}, "yes-token", "0xdemo")
    with pytest.raises(AdapterError):
        normalize_orderbook({k: v for k, v in book().items() if k != "min_order_size"}, "yes-token", "0xdemo")
    with pytest.raises(AdapterError):
        normalize_orderbook(book(), "yes-token", "0xdemo", market(tick_size="0.01"))


def test_resolved_raw_evidence_files_hash_recomputable(tmp_path):
    demo_run(tmp_path, CONFIG)
    raw_dir = tmp_path / "data/forward_v5_1_8/demo/resolved_market_raw"
    files = list(raw_dir.glob("*.json"))
    assert files
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert content_hash(payload["gamma"]["payload"]) == payload["gamma"]["payload_sha256"]
    assert content_hash(payload["clob"]["payload"]) == payload["clob"]["payload_sha256"]
    assert audit_integrity(tmp_path, DEMO, CONFIG)["checks"]["settlement_raw_hash_mismatch"] == 0


def test_market_state_branches():
    assert market_state(market())["market_status"] == "active_trading"
    assert market_state(market(acceptingOrders=False))["market_status"] == "active_not_accepting_orders"
    assert market_state(market(active=False, closed=True, resolved=False))["market_status"] == "resolution_pending"
    assert market_state(market(active=False, closed=True, resolved=True))["market_status"] == "resolved"


def test_rc7_infinite_loop_sleeps_between_three_rounds(monkeypatch, tmp_path):
    calls = []
    sleeps = []

    def fake_monitor(*args, **kwargs):
        calls.append(args[3])
        return {"run_id": args[3], "stopped": len(calls) >= 3}

    monkeypatch.setattr(sim, "_monitor_once_under_lock", fake_monitor)
    result = sim.run_loop(tmp_path, DEMO, CONFIG, iterations=0, interval_seconds=Decimal("60"), run_id="infinite3", confirm_infinite=True, sleep_func=lambda x: sleeps.append(x))
    assert result["iterations_completed"] == 3
    assert sleeps == [60.0, 60.0]


def test_rc7_finite_loop_sleep_counts(monkeypatch, tmp_path):
    calls = []
    sleeps = []
    monkeypatch.setattr(sim, "_monitor_once_under_lock", lambda *a, **k: calls.append(1) or {"run_id": a[3]})
    result = sim.run_loop(tmp_path, DEMO, CONFIG, iterations=3, interval_seconds=Decimal("60"), run_id="finite3", sleep_func=lambda x: sleeps.append(x))
    assert result["iterations_completed"] == 3
    assert len(calls) == 3
    assert sleeps == [60.0, 60.0]


def test_rc7_single_iteration_does_not_sleep(monkeypatch, tmp_path):
    calls = []
    sleeps = []
    monkeypatch.setattr(sim, "_monitor_once_under_lock", lambda *a, **k: calls.append(1) or {"run_id": a[3]})
    result = sim.run_loop(tmp_path, DEMO, CONFIG, iterations=1, interval_seconds=Decimal("60"), run_id="one", sleep_func=lambda x: sleeps.append(x))
    assert result["iterations_completed"] == 1
    assert len(calls) == 1
    assert sleeps == []


def test_rc7_pause_uses_low_frequency_wait_and_no_monitor(monkeypatch, tmp_path):
    init_ledger(tmp_path, DEMO, CONFIG)
    monitor_control(tmp_path, DEMO, CONFIG, "pause")
    calls = []
    sleeps = []
    monkeypatch.setattr(sim, "_monitor_once_under_lock", lambda *a, **k: calls.append(1) or {"run_id": a[3]})

    def fake_sleep(x):
        sleeps.append(x)
        raise KeyboardInterrupt

    result = sim.run_loop(tmp_path, DEMO, CONFIG, iterations=0, interval_seconds=Decimal("0"), run_id="paused", confirm_infinite=True, sleep_func=fake_sleep)
    assert result["interrupted"] is True
    assert calls == []
    assert sleeps == [5.0]


def test_rc7_resume_after_pause_enters_normal_loop(monkeypatch, tmp_path):
    init_ledger(tmp_path, DEMO, CONFIG)
    monitor_control(tmp_path, DEMO, CONFIG, "pause")
    calls = []
    sleeps = []
    monkeypatch.setattr(sim, "_monitor_once_under_lock", lambda *a, **k: calls.append(1) or {"run_id": a[3]})

    def fake_sleep(x):
        sleeps.append(x)
        monitor_control(tmp_path, DEMO, CONFIG, "resume")

    result = sim.run_loop(tmp_path, DEMO, CONFIG, iterations=1, interval_seconds=Decimal("60"), run_id="resume", sleep_func=fake_sleep)
    assert result["iterations_completed"] == 1
    assert calls == [1]
    assert sleeps == [5.0]


def test_rc7_fatal_and_recoverable_errors(monkeypatch, tmp_path):
    sleeps = []
    monkeypatch.setattr(sim, "_monitor_once_under_lock", lambda *a, **k: {"fatal_error": True})
    fatal = sim.run_loop(tmp_path, DEMO, CONFIG, iterations=3, interval_seconds=Decimal("60"), run_id="fatal", sleep_func=lambda x: sleeps.append(x))
    assert fatal["iterations_completed"] == 0
    assert sleeps == []
    monitor_control(tmp_path, DEMO, CONFIG, "resume")
    sequence = [{"recoverable_error": True}, {"stopped": True}]
    monkeypatch.setattr(sim, "_monitor_once_under_lock", lambda *a, **k: sequence.pop(0))
    sleeps = []
    rec = sim.run_loop(tmp_path, DEMO, CONFIG, iterations=0, interval_seconds=Decimal("60"), run_id="recoverable", confirm_infinite=True, sleep_func=lambda x: sleeps.append(x))
    assert rec["iterations_completed"] == 2
    assert sleeps == [2.0]


def test_rc7_ctrl_c_releases_lock_and_marks_stopped(monkeypatch, tmp_path):
    sleeps = []
    monkeypatch.setattr(sim, "monitor_once", lambda *a, **k: {"run_id": k.get("run_id")})

    def interrupting_sleep(x):
        sleeps.append(x)
        raise KeyboardInterrupt

    result = sim.run_loop(tmp_path, DEMO, CONFIG, iterations=2, interval_seconds=Decimal("60"), run_id="ctrlc", sleep_func=interrupting_sleep)
    config = load_config(CONFIG)
    assert result["interrupted"] is True
    assert not lock_path(tmp_path, DEMO, config).exists()
    st = status(tmp_path, DEMO, CONFIG)
    assert st["heartbeat"]["status"] == "stopped_by_user"


def test_rc7_monitor_once_uses_same_lock_and_rejects_second_instance(tmp_path):
    config = load_config(CONFIG)
    init_ledger(tmp_path, DEMO, CONFIG)
    with sim.acquire_monitor_lock(tmp_path, DEMO, config, "held", command="run_loop"):
        with pytest.raises(RuntimeError):
            monitor_once(tmp_path, DEMO, CONFIG, run_id="blocked", adapter=FixtureAdapter(market(), clob(), [book()]))


def test_rc7_formal_rejects_fixture_and_custom_adapter(tmp_path):
    init_ledger(tmp_path, "formal", CONFIG)
    fixture = FixtureAdapter(market(), clob(), [book()])
    result = monitor_once(tmp_path, "formal", CONFIG, run_id="formal_fixture", adapter=fixture)
    assert result["fatal_error"] is True
    assert result["status"] == "formal_adapter_injection_rejected"

    class CustomAdapter(PublicAdapter):
        pass

    result2 = monitor_once(tmp_path, "formal", CONFIG, run_id="formal_custom", adapter=CustomAdapter(transport=lambda u, m, t: (200, "{}")))
    assert result2["fatal_error"] is True


def test_rc7_gamma_real_tick_min_fields_are_read_and_crosschecked():
    gamma = market(orderPriceMinTickSize="0.001", orderMinSize="5")
    norm = normalize_orderbook(book(tick_size="0.001", min_order_size="5"), "yes-token", "0xdemo", gamma)
    assert norm["gamma_tick_size"] == Decimal("0.001")
    assert norm["gamma_min_order_size"] == Decimal("5")
    assert norm["constraint_crosscheck_status"] == "official"
    with pytest.raises(AdapterError):
        normalize_orderbook(book(tick_size="0.001"), "yes-token", "0xdemo", market(orderPriceMinTickSize="0.01", orderMinSize="5"))
    with pytest.raises(AdapterError):
        normalize_orderbook(book(min_order_size="5"), "yes-token", "0xdemo", market(orderPriceMinTickSize="0.001", orderMinSize="10"))


def test_rc7_only_gamma_constraints_do_not_allow_formal_orderbook():
    raw = {k: v for k, v in book().items() if k not in {"tick_size", "min_order_size"}}
    with pytest.raises(AdapterError):
        normalize_orderbook(raw, "yes-token", "0xdemo", market(orderPriceMinTickSize="0.001", orderMinSize="5"))


def test_rc7_proposed_and_auto_resolved_proposed_never_final():
    proposed = market(active=False, closed=True, resolved=True, automaticallyResolved=False, winningOutcome="Yes", outcomePrices=json.dumps(["1", "0"]), umaResolutionStatus="proposed")
    evidence = parse_settlement_evidence(proposed, gamma_token_pairs(proposed))
    assert not evidence["evidence_valid"]
    assert evidence["settlement_status"] == "proposed"
    auto = market(active=False, closed=True, resolved=True, automaticallyResolved=True, winningOutcome="Yes", outcomePrices=json.dumps(["1", "0"]), umaResolutionStatus="proposed")
    evidence2 = parse_settlement_evidence(auto, gamma_token_pairs(auto))
    assert not evidence2["evidence_valid"]
    assert evidence2["settlement_status"] == "conflict"


def test_rc7_winning_asset_is_strong_final_evidence():
    resolved = market(active=False, closed=True, resolved=True, winningAssetId="yes-token", umaResolutionStatus="final")
    evidence = parse_settlement_evidence(resolved, gamma_token_pairs(resolved))
    assert evidence["evidence_valid"]
    assert evidence["evidence_tier"] == "A_winning_asset_id"


def test_rc7_clob_public_token_winner_is_strong_final_evidence():
    resolved = market(active=False, closed=True, resolved=True, umaResolutionStatus="resolved", outcomePrices=json.dumps(["1", "0"]))
    clob_public = {
        "tokens": [
            {"token_id": "yes-token", "outcome": "Yes", "price": 1, "winner": True},
            {"token_id": "no-token", "outcome": "No", "price": 0, "winner": False},
        ]
    }
    evidence = parse_settlement_evidence(resolved, clob_token_pairs(clob_public))
    assert evidence["evidence_valid"]
    assert evidence["evidence_tier"] == "A_clob_token_winner"
    assert evidence["token_settlement_values"]["yes-token"] == "1"


def beijing_market(bucket_text="26°C", slug_bucket="26c", condition_id="0xbeijing"):
    return {
        "question": f"Will the highest temperature in Beijing be {bucket_text} on July 22?",
        "title": f"Will the highest temperature in Beijing be {bucket_text} on July 22?",
        "slug": f"highest-temperature-in-beijing-on-july-22-2026-{slug_bucket}",
        "conditionId": condition_id,
        "outcomes": json.dumps(["Yes", "No"]),
        "clobTokenIds": json.dumps([condition_id + "-yes", condition_id + "-no"]),
        "active": True,
        "closed": False,
        "resolved": False,
        "acceptingOrders": True,
        "feesEnabled": True,
        "feeSchedule": {"rate": "0.05", "exponent": "1"},
        "endDate": "2026-07-22T23:59:00Z",
        "groupItemTitle": bucket_text,
    }


def validation_book_for(m):
    token = json.loads(m["clobTokenIds"])[0]
    return normalize_orderbook({"market": m["conditionId"], "asset_id": token, "bids": [{"price": "0.10", "size": "100"}], "asks": [{"price": "0.20", "size": "100"}], "tick_size": "0.001", "min_order_size": "5"}, token, m["conditionId"], m)


def test_rc7_beijing_question_city_parses_without_temperature_suffix():
    parsed = parse_weather_market(beijing_market("26°C", "26c"))
    assert parsed["parsing_status"] == "ok"
    assert parsed["city"] == "Beijing"
    assert parsed["weather_date_local"] == "2026-07-22"
    assert parsed["weather_metric"] == "high"
    assert parsed["canonical_label"] == "exact:26C"


def test_rc7_second_city_question_parses():
    m = beijing_market("30°C", "30c", "0xtokyo")
    m["question"] = "Will the highest temperature in Tokyo be 30°C on July 22?"
    m["title"] = m["question"]
    m["slug"] = "highest-temperature-in-tokyo-on-july-22-2026-30c"
    parsed = parse_weather_market(m)
    assert parsed["city"] == "Tokyo"
    assert parsed["event_key"] == "tokyo|2026-07-22|high"


def test_rc7_temperature_bucket_regressions():
    expected = {
        "30C": "exact:30C",
        "20C": "exact:20C",
        "10C": "exact:10C",
        "100F": "exact:100F",
        "0C": "exact:0C",
        "-10C": "exact:-10C",
        "30.0C": "exact:30C",
        "30.50C": "exact:30.5C",
        "25C or below": "or_below:25C",
        "25C or lower": "or_below:25C",
        "35C or higher": "or_higher:35C",
        "35C or above": "or_higher:35C",
        "exact:30C": "exact:30C",
        "or_below:29C": "or_below:29C",
        "or_higher:35C": "or_higher:35C",
    }
    for raw, canonical in expected.items():
        assert parse_temperature_bucket(raw) == canonical


def test_rc7_exact_or_below_or_higher_are_distinct():
    assert parse_temperature_bucket("25C") != parse_temperature_bucket("25C or below")
    assert parse_temperature_bucket("25C") != parse_temperature_bucket("25C or higher")


def test_rc7_real_beijing_exact_signal_passes_and_boundary_rejects():
    m = beijing_market("26°C", "26c", "0xbeijing26")
    token = json.loads(m["clobTokenIds"])[0]
    clob_info = {"condition_id": "0xbeijing26", "t": [{"t": token, "o": "Yes"}, {"t": "0xbeijing26-no", "o": "No"}]}
    base_signal = {"city": "Beijing", "weather_date_local": "2026-07-22", "weather_metric": "high", "temperature_bucket": "26C", "condition_id": "0xbeijing26", "token_id": token, "outcome": "Yes"}
    assert validate_token_mapping(base_signal, m, clob_info, validation_book_for(m))["mapping_valid"]
    bad = validate_token_mapping({**base_signal, "temperature_bucket": "26C or below"}, m, clob_info, validation_book_for(m))
    assert not bad["mapping_valid"]
    assert "BUCKET_TYPE_MISMATCH" in bad["errors"]


def test_rc7_beijing_or_below_signal_passes_and_exact_rejects():
    m = beijing_market("25°C or below", "25corbelow", "0xbeijing25below")
    token = json.loads(m["clobTokenIds"])[0]
    clob_info = {"condition_id": "0xbeijing25below", "t": [{"t": token, "o": "Yes"}, {"t": "0xbeijing25below-no", "o": "No"}]}
    base_signal = {"city": "Beijing", "weather_date_local": "2026-07-22", "weather_metric": "high", "temperature_bucket": "25C or below", "condition_id": "0xbeijing25below", "token_id": token, "outcome": "Yes"}
    assert validate_token_mapping(base_signal, m, clob_info, validation_book_for(m))["mapping_valid"]
    exact = validate_token_mapping({**base_signal, "temperature_bucket": "25C"}, m, clob_info, validation_book_for(m))
    assert not exact["mapping_valid"]
    assert "BUCKET_TYPE_MISMATCH" in exact["errors"]


def test_rc7_accepting_orders_missing_and_conflict_fail_closed():
    missing = market()
    for key in ["acceptingOrders", "accepting_orders", "enableOrderBook"]:
        missing.pop(key, None)
    assert market_state(missing)["market_status"] == "active_accepting_orders_unknown"
    assert market_state(market(acceptingOrders=True))["market_status"] == "active_trading"
    assert market_state(market(acceptingOrders=False))["market_status"] == "active_not_accepting_orders"
    assert market_state(market(acceptingOrders=True), {"acceptingOrders": False})["market_status"] == "status_conflict"
    assert market_state(market(acceptingOrders=True, enableOrderBook=False))["market_status"] == "status_conflict"


def test_rc7_lock_recovery_active_pid_stale_is_not_recoverable():
    config = load_config(CONFIG)
    info = {
        "pid": sim.os.getpid(),
        "hostname": sim.socket.gethostname(),
        "process_start_time": sim.PROCESS_START_TIME,
        "heartbeat_at_utc": (sim.utcnow() - timedelta(seconds=int(config["execution"]["lock_stale_seconds"]) + 60)).isoformat(),
    }
    decision = lock_recovery_decision(info, config)
    assert decision["recoverable"] is False
    assert decision["reason"] == "active_pid"


def test_rc7_lock_recovery_dead_pid_is_recoverable():
    config = load_config(CONFIG)
    info = {
        "pid": 999999,
        "hostname": sim.socket.gethostname(),
        "process_start_time": "2000-01-01T00:00:00+00:00",
        "heartbeat_at_utc": (sim.utcnow() - timedelta(seconds=int(config["execution"]["lock_stale_seconds"]) + 60)).isoformat(),
    }
    assert lock_recovery_decision(info, config)["recoverable"] is True


def test_rc7_internal_monitor_requires_valid_lock_token(tmp_path):
    with pytest.raises(RuntimeError):
        sim._monitor_once_under_lock(tmp_path, DEMO, CONFIG, "no_token", None)


def test_rc7_forged_and_old_lock_tokens_rejected(tmp_path):
    config = load_config(CONFIG)
    with sim.acquire_monitor_lock(tmp_path, DEMO, config, "locked", command="pytest") as token:
        forged = {**token, "nonce": "bad"}
        with pytest.raises(RuntimeError):
            sim._monitor_once_under_lock(tmp_path, DEMO, CONFIG, "locked", forged)
        old = {**token, "run_id": "other"}
        with pytest.raises(RuntimeError):
            sim._monitor_once_under_lock(tmp_path, DEMO, CONFIG, "locked", old)


def test_rc7_public_monitor_rejects_reentry_while_lock_held(tmp_path):
    config = load_config(CONFIG)
    with sim.acquire_monitor_lock(tmp_path, DEMO, config, "outer", command="pytest"):
        with pytest.raises(RuntimeError):
            monitor_once(tmp_path, DEMO, CONFIG, run_id="inner")


def test_rc7_audit_recomputes_gamma_and_clob_hashes(tmp_path):
    demo_run(tmp_path, CONFIG)
    config = load_config(CONFIG)
    conn = connect(db_path(tmp_path, DEMO, config))
    with conn:
        conn.execute("UPDATE settlements SET raw_response='{\"tampered\":true}'")
    conn.close()
    audit = audit_integrity(tmp_path, DEMO, CONFIG)
    assert not audit["ok"]
    assert audit["checks"]["SETTLEMENT_GAMMA_HASH_MISMATCH"] > 0


def test_rc7_audit_detects_clob_hash_tamper(tmp_path):
    demo_run(tmp_path, CONFIG)
    config = load_config(CONFIG)
    conn = connect(db_path(tmp_path, DEMO, config))
    with conn:
        conn.execute("UPDATE settlements SET raw_clob_response='{\"tampered\":true}'")
    conn.close()
    audit = audit_integrity(tmp_path, DEMO, CONFIG)
    assert not audit["ok"]
    assert audit["checks"]["SETTLEMENT_CLOB_HASH_MISMATCH"] > 0


def test_rc7_audit_detects_proposed_final_and_value_tamper(tmp_path):
    demo_run(tmp_path, CONFIG)
    config = load_config(CONFIG)
    conn = connect(db_path(tmp_path, DEMO, config))
    raw = json.loads(conn.execute("SELECT raw_response FROM settlements LIMIT 1").fetchone()["raw_response"])
    raw["umaResolutionStatus"] = "proposed"
    with conn:
        conn.execute("UPDATE settlements SET raw_response=?, raw_response_hash=?, finality_status='resolved_final', uma_status='proposed'", (json.dumps(raw), content_hash(raw)))
    conn.close()
    audit = audit_integrity(tmp_path, DEMO, CONFIG)
    assert not audit["ok"]
    assert audit["checks"]["SETTLEMENT_PROPOSED_MARKED_FINAL"] > 0

    other = tmp_path / "value"
    demo_run(other, CONFIG)
    conn = connect(db_path(other, DEMO, config))
    with conn:
        conn.execute("UPDATE settlements SET settlement_value='0'")
    conn.close()
    audit2 = audit_integrity(other, DEMO, CONFIG)
    assert not audit2["ok"]
    assert audit2["checks"]["SETTLEMENT_VALUE_MISMATCH"] > 0


def test_rc7_negative_audit_framework_is_direct_and_detects_all(tmp_path):
    from src.forward_reporting_v5_1_8 import write_negative_tests_csv

    rows = write_negative_tests_csv(tmp_path, CONFIG)
    assert len(rows) == 30
    assert all(row["direct_business_data_modified"] == "true" for row in rows)
    assert all(row["synthetic_violation_event_inserted"] == "false" for row in rows)
    assert all(row["full_replay_executed"] == "true" for row in rows)
    assert all(row["detected"] == "true" for row in rows)
    assert [row for row in rows if row["corruption_case"].startswith("30_")][0]["followup_extra_fill_created"] == "false"


def test_v517_illegal_bucket_rejected_at_registration(tmp_path):
    fields = [
        "signal_id",
        "created_at_utc",
        "city",
        "weather_date_local",
        "weather_metric",
        "bucket_type",
        "temperature_threshold",
        "temperature_unit",
        "market_slug",
        "condition_id",
        "token_id",
        "outcome",
        "side",
        "intended_usd",
        "max_entry_price",
        "forecast_probability",
        "source",
        "notes",
    ]
    signal_file = tmp_path / "bad_bucket.csv"
    with signal_file.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fields)
        writer.writeheader()
        writer.writerow(
            {
                "signal_id": "bad-bucket",
                "created_at_utc": "2099-01-01T00:00:00+00:00",
                "city": "Demo City",
                "weather_date_local": "2099-01-02",
                "weather_metric": "high",
                "bucket_type": "nonsense",
                "temperature_threshold": "30",
                "temperature_unit": "C",
                "market_slug": "highest-temperature-in-demo-city-on-january-2-2099-30c",
                "condition_id": "0xdemo",
                "token_id": "yes-token",
                "outcome": "Yes",
                "side": "BUY",
                "intended_usd": "10",
                "max_entry_price": "0.3",
                "forecast_probability": "0.6",
                "source": "pytest",
                "notes": "",
            }
        )
    rows = register_signals(tmp_path, DEMO, CONFIG, signal_file, now=parse_utc("2099-01-01T00:00:01+00:00"))
    assert rows == []


@pytest.mark.parametrize("raw,expected", [("-1C", "exact:-1C"), ("-10C", "exact:-10C"), ("-20F", "exact:-20F")])
def test_v517_negative_temperature_buckets(raw, expected):
    assert parse_temperature_bucket(raw) == expected


@pytest.mark.parametrize("case,expected_error", [
    ("01_modify_gamma_fee_raw_response", "MARKET_RAW_HTTP_HASH_MISMATCH"),
    ("02_modify_clob_fee_raw_response", "MARKET_RAW_HTTP_HASH_MISMATCH"),
    ("03_change_fill_and_lot_fee_005_to_010", "FILL_FEE_RATE_MISMATCH"),
    ("04_modify_signal_intended_usd", "SIGNAL_INTENDED_USD_MISMATCH"),
    ("05_modify_signal_max_entry_price", "SIGNAL_MAX_ENTRY_PRICE_MISMATCH"),
    ("06_modify_signal_entry_deadline", "SIGNAL_ENTRY_DEADLINE_MISMATCH"),
    ("07_modify_signal_event_key", "SIGNAL_EVENT_KEY_MISMATCH"),
    ("08_modify_signal_bucket", "SIGNAL_BUCKET_MISMATCH"),
    ("09_delete_signal_registration_evidence", "SIGNAL_REGISTRATION_EVIDENCE_MISSING"),
    ("10_modify_signal_canonical_hash", "SIGNAL_CANONICAL_HASH_MISMATCH"),
    ("11_forge_entry_state_remaining_usd", "ENTRY_STATE_REMAINING_USD_MISMATCH"),
    ("12_reopen_filled_signal_as_partial", "ENTRY_STATE_REOPENED_AFTER_FILLED"),
    ("13_reopen_expired_signal_as_pending", "ENTRY_STATE_REOPENED_AFTER_FILLED"),
    ("14_modify_entry_state_shares", "ENTRY_STATE_SHARES_MISMATCH"),
    ("15_modify_strategy_lot_entry_fee", "LOT_ENTRY_FEE_MISMATCH"),
    ("16_modify_strategy_lot_shares", "LOT_ENTRY_SHARES_MISMATCH"),
    ("17_modify_strategy_lot_remaining_shares", "LOT_REMAINING_SHARES_MISMATCH"),
    ("18_modify_strategy_lot_net_pnl", "LOT_PNL_MISMATCH"),
    ("19_modify_exit_allocation_shares", "EXIT_ALLOCATION_SHARES_MISMATCH"),
    ("20_modify_exit_allocation_net_proceeds", "EXIT_ALLOCATION_NET_PROCEEDS_MISMATCH"),
    ("21_modify_settlement_allocation_shares", "SETTLEMENT_ALLOCATION_SHARES_MISMATCH"),
    ("22_modify_settlement_allocation_net_proceeds", "SETTLEMENT_ALLOCATION_NET_PROCEEDS_MISMATCH"),
    ("23_modify_event_result_net_pnl", "EVENT_PNL_MISMATCH"),
    ("24_modify_strategy_total_pnl", "STRATEGY_PNL_MISMATCH"),
    ("25_modify_total_ledger_pnl", "TOTAL_LEDGER_PNL_MISMATCH"),
    ("26_modify_snapshot_selected_tick", "MARKET_TICK_SIZE_MISMATCH"),
    ("27_modify_snapshot_selected_min_order", "MARKET_MIN_ORDER_SIZE_MISMATCH"),
    ("28_modify_constraint_hash", "MARKET_CONSTRAINT_HASH_MISMATCH"),
    ("29_incomplete_take_profit_false_positive", "INCOMPLETE_TAKE_PROFIT_MISMATCH"),
    ("30_forge_partial_then_monitor_no_extra_buy", "ENTRY_STATE_REOPENED_AFTER_FILLED"),
])
def test_v518_full_replay_detects_each_end_to_end_corruption(tmp_path, case, expected_error):
    from src.forward_reporting_v5_1_8 import apply_case

    demo_run(tmp_path, CONFIG)
    config = load_config(CONFIG)
    conn = connect(db_path(tmp_path, DEMO, config))
    try:
        apply_case(conn, case)
    finally:
        conn.close()
    if case == "30_forge_partial_then_monitor_no_extra_buy":
        market_data, clob_data, books, _ = sim.demo_fixture()
        before = connect(db_path(tmp_path, DEMO, config))
        try:
            before_count = before.execute("SELECT COUNT(*) c FROM entry_fills").fetchone()["c"]
        finally:
            before.close()
        monitor_once(tmp_path, DEMO, CONFIG, run_id="pytest_followup_after_partial_forgery", adapter=sim.FixtureAdapter(market_data, clob_data, [books[0]]), now=parse_utc("2099-01-01T00:00:04+00:00"))
        after = connect(db_path(tmp_path, DEMO, config))
        try:
            assert after.execute("SELECT COUNT(*) c FROM entry_fills").fetchone()["c"] == before_count
        finally:
            after.close()
    audit = audit_integrity(tmp_path, DEMO, CONFIG, "full-replay")
    assert not audit["ok"]
    assert audit["checks"].get(expected_error, 0) > 0


def test_v518_exact_http_bytes_hash_is_checked(tmp_path):
    demo_run(tmp_path, CONFIG)
    config = load_config(CONFIG)
    conn = connect(db_path(tmp_path, DEMO, config))
    try:
        row = conn.execute("SELECT evidence_id,raw_http_bytes,raw_http_sha256 FROM http_evidence WHERE evidence_type='orderbook' ORDER BY rowid LIMIT 1").fetchone()
        assert row["raw_http_sha256"] == __import__("hashlib").sha256(bytes(row["raw_http_bytes"])).hexdigest()
        conn.execute("UPDATE http_evidence SET raw_http_bytes=? WHERE evidence_id=?", (b"{\"tampered\":true}", row["evidence_id"]))
        conn.commit()
    finally:
        conn.close()
    audit = audit_integrity(tmp_path, DEMO, CONFIG, "full-replay")
    assert audit["checks"]["ORDERBOOK_RAW_HTTP_HASH_MISMATCH"] > 0


def test_v518_latest_trigger_state_controls_incomplete_report(tmp_path):
    demo_run(tmp_path, CONFIG)
    config = load_config(CONFIG)
    conn = connect(db_path(tmp_path, DEMO, config))
    try:
        rows = conn.execute("SELECT incomplete_take_profit FROM event_results WHERE strategy_id='tp_2x_sell_50pct'").fetchall()
        assert all(r["incomplete_take_profit"] == 0 for r in rows)
        conn.execute("UPDATE event_results SET incomplete_take_profit=1 WHERE strategy_id='tp_2x_sell_50pct'")
        conn.commit()
    finally:
        conn.close()
    audit = audit_integrity(tmp_path, DEMO, CONFIG, "full-replay")
    assert audit["checks"]["INCOMPLETE_TAKE_PROFIT_MISMATCH"] > 0


def _live_fixture_config(tmp_path: Path, slug: str) -> Path:
    text = CONFIG.read_text(encoding="utf-8")
    out = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.strip() == "preferred_weather_slugs:":
            out.append("  preferred_weather_slugs:")
            out.append(f"    - {slug}")
            i += 1
            while i < len(lines) and lines[i].startswith("    - "):
                i += 1
            continue
        if line.strip() == "resolved_weather_slugs:":
            out.append("  resolved_weather_slugs: []")
            i += 1
            while i < len(lines) and lines[i].startswith("    - "):
                i += 1
            continue
        out.append(line)
        i += 1
    cfg = tmp_path / "config" / "forward_simulation_v5_1_8.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("\n".join(out) + "\n", encoding="utf-8")
    return cfg


def _run_fixture_live(tmp_path: Path):
    market_data, clob_data, books, _ = demo_fixture()
    slug = market_data["slug"]
    cfg = _live_fixture_config(tmp_path, slug)
    adapter = FixtureAdapter(market_data, clob_data, books)
    manifest = sim.live_integration(tmp_path, cfg, iterations=1, interval_seconds=Decimal("0"), run_id="live_fixture_rc7", adapter=adapter)
    signal = json.loads((tmp_path / "data/forward_v5_1_8/rc7/real_signal_to_fill_validation.json").read_text(encoding="utf-8"))
    return manifest, signal, cfg


def test_persist_http_result_roundtrip_with_raw_bytes(tmp_path):
    from src.polymarket_public_adapter_v5_1_8 import HttpResult, persist_http_result, verify_http_evidence_file, iso
    from decimal import Decimal as D

    payload = {"asks": [{"price": "0.1", "size": "10"}], "bids": []}
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    result = HttpResult(
        method="GET",
        url="https://example.test/book",
        status_code=200,
        latency_ms=D("1.5"),
        started_at_utc=iso(),
        received_at_utc=iso(),
        payload=payload,
        raw_text=raw.decode("utf-8"),
        raw_bytes=raw,
        content_type="application/json",
    )
    meta = persist_http_result(tmp_path, tmp_path / "e.json", tmp_path / "e.bin", result)
    assert (tmp_path / "e.bin").read_bytes() == raw
    assert meta["raw_bytes_sha256"] == __import__("hashlib").sha256(raw).hexdigest()
    verified = verify_http_evidence_file(tmp_path, tmp_path / "e.json")
    assert verified["ok"]
    assert verified["raw_bytes"] == raw


def test_live_fixture_writes_durable_raw_evidence(tmp_path):
    manifest, signal, _ = _run_fixture_live(tmp_path)
    assert manifest["error_count"] == 0
    assert manifest["snapshot_count"] > 0
    assert manifest["raw_orderbook_evidence_count"] == manifest["snapshot_count"]
    assert signal["status"] == "pass"
    assert signal["validation_source"] == "live_readonly_saved_evidence"
    assert signal["uses_formal_ledger"] is False
    assert signal["uses_wallet_or_real_order"] is False
    out_dir = tmp_path / "data/forward_v5_1_8/live_integration" / manifest["run_id"]
    indexes = sorted((out_dir / "raw_orderbooks").glob("*_index.json"))
    assert len(indexes) == manifest["snapshot_count"]
    for idx in indexes:
        data = json.loads(idx.read_text(encoding="utf-8"))
        for key in ("gamma_evidence_path", "clob_evidence_path", "orderbook_evidence_path"):
            meta_path = tmp_path / data[key]
            assert meta_path.exists()
            bin_rel = json.loads(meta_path.read_text(encoding="utf-8"))["raw_bytes_path"]
            assert (tmp_path / bin_rel).exists()
            from src.polymarket_public_adapter_v5_1_8 import verify_http_evidence_file

            assert verify_http_evidence_file(tmp_path, meta_path)["ok"]
    # No formal ledger created by live-readonly.
    assert not (tmp_path / "data/forward_v5_1_8/formal").exists()


def test_release_blocked_when_raw_orderbook_evidence_deleted(tmp_path):
    from src.forward_reporting_v5_1_8 import compute_release_status

    manifest, signal, _ = _run_fixture_live(tmp_path)
    out_dir = tmp_path / "data/forward_v5_1_8/live_integration" / manifest["run_id"]
    target = next((out_dir / "raw_orderbooks").glob("*_orderbook.bin"))
    target.unlink()
    release = compute_release_status(
        manifest,
        signal,
        {"status": "pass"},
        {"ok": True},
        {"ok": True},
        {"ok": True, "formal_started_at_utc": None},
        30,
        root=tmp_path,
    )
    assert release["release_status"] == "BLOCKED_PENDING_LIVE_EVIDENCE"
    assert release["blocked_reasons"]


def test_release_blocked_when_error_count_forged(tmp_path):
    from src.forward_reporting_v5_1_8 import compute_release_status

    manifest, signal, _ = _run_fixture_live(tmp_path)
    forged = dict(manifest)
    forged["error_count"] = 1
    release = compute_release_status(
        forged,
        signal,
        {"status": "pass"},
        {"ok": True},
        {"ok": True},
        {"ok": True, "formal_started_at_utc": None},
        30,
        root=tmp_path,
    )
    assert release["release_status"] == "BLOCKED_PENDING_LIVE_EVIDENCE"
    assert any("error_count" in r for r in release["blocked_reasons"])


def test_release_blocked_when_raw_bytes_tampered(tmp_path):
    from src.forward_reporting_v5_1_8 import compute_release_status

    manifest, signal, _ = _run_fixture_live(tmp_path)
    out_dir = tmp_path / "data/forward_v5_1_8/live_integration" / manifest["run_id"]
    target = next((out_dir / "raw_orderbooks").glob("*_orderbook.bin"))
    target.write_bytes(b'{"tampered":true}')
    release = compute_release_status(
        manifest,
        signal,
        {"status": "pass"},
        {"ok": True},
        {"ok": True},
        {"ok": True, "formal_started_at_utc": None},
        30,
        root=tmp_path,
    )
    assert release["release_status"] == "BLOCKED_PENDING_LIVE_EVIDENCE"
    assert any(("hash" in r) or ("binding" in r) or ("mismatch" in r) for r in release["blocked_reasons"])


def test_release_blocked_when_normalized_snapshot_tampered(tmp_path):
    from src.forward_reporting_v5_1_8 import compute_release_status

    manifest, signal, _ = _run_fixture_live(tmp_path)
    out_dir = tmp_path / "data/forward_v5_1_8/live_integration" / manifest["run_id"]
    snap_path = out_dir / "orderbook_snapshots.jsonl"
    rows = [json.loads(line) for line in snap_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows[0]["content_hash"] = "0" * 64
    snap_path.write_text("\n".join(json.dumps(r, sort_keys=True) for r in rows) + "\n", encoding="utf-8")
    release = compute_release_status(
        manifest,
        signal,
        {"status": "pass"},
        {"ok": True},
        {"ok": True},
        {"ok": True, "formal_started_at_utc": None},
        30,
        root=tmp_path,
    )
    assert release["release_status"] == "BLOCKED_PENDING_LIVE_EVIDENCE"
    assert any("replay" in r for r in release["blocked_reasons"])


def test_release_blocked_when_signal_references_other_run(tmp_path):
    from src.forward_reporting_v5_1_8 import compute_release_status

    manifest, signal, _ = _run_fixture_live(tmp_path)
    bad = dict(signal)
    bad["run_id"] = "some_other_run"
    release = compute_release_status(
        manifest,
        bad,
        {"status": "pass"},
        {"ok": True},
        {"ok": True},
        {"ok": True, "formal_started_at_utc": None},
        30,
        root=tmp_path,
    )
    assert release["release_status"] == "BLOCKED_PENDING_LIVE_EVIDENCE"
    assert any("run_id" in r for r in release["blocked_reasons"])


def test_release_pass_with_complete_same_run_evidence_chain(tmp_path):
    from src.forward_reporting_v5_1_8 import compute_release_status

    manifest, signal, _ = _run_fixture_live(tmp_path)
    release = compute_release_status(
        manifest,
        signal,
        {"status": "pass"},
        {"ok": True},
        {"ok": True},
        {"ok": True, "formal_started_at_utc": None},
        30,
        root=tmp_path,
    )
    assert release["release_status"] == "PASS_FOR_FORMAL_START"
    assert release["blocked_reasons"] == []
    assert release["raw_evidence_hash_result"] == "pass"
    assert release["snapshot_replay_result"] == "pass"
    assert release["same_run_evidence_chain"] is True


def _make_http_result(payload, raw=None):
    from src.polymarket_public_adapter_v5_1_8 import HttpResult, iso
    from decimal import Decimal as D
    if raw is None:
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if isinstance(raw, str):
        raw_b = raw.encode("utf-8")
        raw_t = raw
    else:
        raw_b = raw
        raw_t = raw.decode("utf-8")
    return HttpResult(
        method="GET",
        url="https://example.test/book",
        status_code=200,
        latency_ms=D("1"),
        started_at_utc=iso(),
        received_at_utc=iso(),
        payload=payload,
        raw_text=raw_t,
        raw_bytes=raw_b,
        content_type="application/json",
    )


def test_raw_payload_binding_pass_when_consistent(tmp_path):
    from src.polymarket_public_adapter_v5_1_8 import persist_http_result, verify_http_evidence_file, content_hash

    payload = {"asks": [{"price": "0.2", "size": "5"}], "bids": []}
    meta = persist_http_result(tmp_path, tmp_path / "ok.json", tmp_path / "ok.bin", _make_http_result(payload))
    verified = verify_http_evidence_file(tmp_path, tmp_path / "ok.json")
    assert verified["ok"]
    assert verified["parsed_payload_from_raw"] == payload
    assert verified["parsed_raw_payload_sha256"] == content_hash(payload)
    assert verified["parsed_raw_payload_sha256"] == meta["payload_sha256"]


def test_raw_payload_binding_blocked_when_metadata_payload_tampered_without_hash(tmp_path):
    from src.polymarket_public_adapter_v5_1_8 import persist_http_result, verify_http_evidence_file, write_json

    payload = {"ok": True, "n": 1}
    persist_http_result(tmp_path, tmp_path / "e.json", tmp_path / "e.bin", _make_http_result(payload))
    meta = json.loads((tmp_path / "e.json").read_text(encoding="utf-8"))
    meta["payload"] = {"ok": False, "n": 999}
    write_json(tmp_path / "e.json", meta)
    verified = verify_http_evidence_file(tmp_path, tmp_path / "e.json")
    assert not verified["ok"]
    assert "metadata_payload_differs_from_raw" in verified["errors"] or "metadata_payload_sha256_mismatch" in verified["errors"]


def test_raw_payload_binding_blocked_when_metadata_payload_and_hash_rewritten(tmp_path):
    from src.polymarket_public_adapter_v5_1_8 import persist_http_result, verify_http_evidence_file, content_hash, write_json

    payload = {"ok": True, "n": 1}
    persist_http_result(tmp_path, tmp_path / "e.json", tmp_path / "e.bin", _make_http_result(payload))
    meta = json.loads((tmp_path / "e.json").read_text(encoding="utf-8"))
    forged = {"ok": False, "forged": True}
    meta["payload"] = forged
    meta["payload_sha256"] = content_hash(forged)
    write_json(tmp_path / "e.json", meta)
    verified = verify_http_evidence_file(tmp_path, tmp_path / "e.json")
    assert not verified["ok"]
    assert "metadata_payload_differs_from_raw" in verified["errors"] or "parsed_raw_payload_sha256_mismatch" in verified["errors"]


def test_raw_payload_binding_blocked_when_bin_rewritten_with_matching_hashes(tmp_path):
    from src.polymarket_public_adapter_v5_1_8 import persist_http_result, verify_http_evidence_file, content_hash, write_json
    from hashlib import sha256

    payload = {"ok": True, "n": 1}
    persist_http_result(tmp_path, tmp_path / "e.json", tmp_path / "e.bin", _make_http_result(payload))
    forged_payload = {"ok": False, "bin_forged": True}
    forged_raw = json.dumps(forged_payload, separators=(",", ":")).encode("utf-8")
    (tmp_path / "e.bin").write_bytes(forged_raw)
    meta = json.loads((tmp_path / "e.json").read_text(encoding="utf-8"))
    meta["raw_bytes_sha256"] = sha256(forged_raw).hexdigest()
    meta["raw_bytes_length"] = len(forged_raw)
    meta["raw_text_sha256"] = sha256(forged_raw).hexdigest()
    # leave metadata payload pointing at original
    write_json(tmp_path / "e.json", meta)
    verified = verify_http_evidence_file(tmp_path, tmp_path / "e.json")
    assert not verified["ok"]
    assert "metadata_payload_differs_from_raw" in verified["errors"] or "parsed_raw_payload_sha256_mismatch" in verified["errors"]


def test_raw_payload_binding_blocked_on_invalid_utf8(tmp_path):
    from src.polymarket_public_adapter_v5_1_8 import persist_http_result, verify_http_evidence_file, write_json
    from hashlib import sha256

    payload = {"ok": True}
    persist_http_result(tmp_path, tmp_path / "e.json", tmp_path / "e.bin", _make_http_result(payload))
    bad = b"\xff\xfe not utf8"
    (tmp_path / "e.bin").write_bytes(bad)
    meta = json.loads((tmp_path / "e.json").read_text(encoding="utf-8"))
    meta["raw_bytes_sha256"] = sha256(bad).hexdigest()
    meta["raw_bytes_length"] = len(bad)
    write_json(tmp_path / "e.json", meta)
    verified = verify_http_evidence_file(tmp_path, tmp_path / "e.json")
    assert not verified["ok"]
    assert "raw_bytes_utf8_decode_failed" in verified["errors"]


def test_raw_payload_binding_blocked_on_invalid_json(tmp_path):
    from src.polymarket_public_adapter_v5_1_8 import persist_http_result, verify_http_evidence_file, write_json
    from hashlib import sha256

    payload = {"ok": True}
    persist_http_result(tmp_path, tmp_path / "e.json", tmp_path / "e.bin", _make_http_result(payload))
    bad = b"not-json{"
    (tmp_path / "e.bin").write_bytes(bad)
    meta = json.loads((tmp_path / "e.json").read_text(encoding="utf-8"))
    meta["raw_bytes_sha256"] = sha256(bad).hexdigest()
    meta["raw_bytes_length"] = len(bad)
    meta["raw_text_sha256"] = sha256(bad).hexdigest()
    write_json(tmp_path / "e.json", meta)
    verified = verify_http_evidence_file(tmp_path, tmp_path / "e.json")
    assert not verified["ok"]
    assert "raw_bytes_json_decode_failed" in verified["errors"]


def test_raw_payload_binding_blocked_when_raw_text_hash_tampered(tmp_path):
    from src.polymarket_public_adapter_v5_1_8 import persist_http_result, verify_http_evidence_file, write_json

    payload = {"ok": True}
    persist_http_result(tmp_path, tmp_path / "e.json", tmp_path / "e.bin", _make_http_result(payload))
    meta = json.loads((tmp_path / "e.json").read_text(encoding="utf-8"))
    meta["raw_text_sha256"] = "0" * 64
    write_json(tmp_path / "e.json", meta)
    verified = verify_http_evidence_file(tmp_path, tmp_path / "e.json")
    assert not verified["ok"]
    assert "raw_text_sha256_mismatch" in verified["errors"]


def test_persist_http_result_rejects_raw_payload_mismatch(tmp_path):
    from src.polymarket_public_adapter_v5_1_8 import persist_http_result, RawHttpPayloadMismatch

    payload = {"ok": True}
    raw = json.dumps({"ok": False, "different": True}, separators=(",", ":")).encode("utf-8")
    result = _make_http_result(payload, raw=raw)
    # force raw_text to match raw bytes so only payload binding fails
    result.raw_text = raw.decode("utf-8")
    result.raw_bytes = raw
    with pytest.raises(RawHttpPayloadMismatch):
        persist_http_result(tmp_path, tmp_path / "bad.json", tmp_path / "bad.bin", result)
    assert not (tmp_path / "bad.json").exists()
    assert not (tmp_path / "bad.bin").exists()


def test_snapshot_and_signal_replay_use_parsed_payload_from_raw(tmp_path, monkeypatch):
    from src.polymarket_public_adapter_v5_1_8 import verify_http_evidence_file

    manifest, signal, _ = _run_fixture_live(tmp_path)
    assert signal["uses_parsed_payload_from_raw"] is True
    assert signal["status"] == "pass"
    assert manifest.get("raw_payload_binding_result") == "pass"
    assert manifest.get("raw_payload_binding_failed_count") == 0
    assert not (tmp_path / "data/forward_v5_1_8/formal").exists()

    captured = {"normalize": [], "validate": []}
    real_norm = sim.normalize_orderbook
    real_validate = sim.validate_token_mapping
    real_verify = sim.verify_http_evidence_file

    def poison_meta_verify(root, path):
        verified = real_verify(root, path)
        if verified.get("ok"):
            verified = dict(verified)
            verified["meta"] = dict(verified.get("meta") or {})
            verified["meta"]["payload"] = {"MUST_NOT_BE_USED_AS_AUTHORITY": True}
        return verified

    def spy_norm(book, *args, **kwargs):
        captured["normalize"].append(book)
        assert book != {"MUST_NOT_BE_USED_AS_AUTHORITY": True}
        return real_norm(book, *args, **kwargs)

    def spy_validate(signal_row, gamma, clob, normalized):
        captured["validate"].append((gamma, clob))
        assert gamma != {"MUST_NOT_BE_USED_AS_AUTHORITY": True}
        assert clob != {"MUST_NOT_BE_USED_AS_AUTHORITY": True}
        return real_validate(signal_row, gamma, clob, normalized)

    monkeypatch.setattr(sim, "verify_http_evidence_file", poison_meta_verify)
    monkeypatch.setattr(sim, "normalize_orderbook", spy_norm)
    monkeypatch.setattr(sim, "validate_token_mapping", spy_validate)

    selected = json.loads((tmp_path / "data/forward_v5_1_8/live_integration" / manifest["run_id"] / "selected_markets.json").read_text(encoding="utf-8"))
    snapshots = [json.loads(line) for line in (tmp_path / "data/forward_v5_1_8/live_integration" / manifest["run_id"] / "orderbook_snapshots.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    rebuilt = sim.build_signal_to_fill_from_saved_evidence(tmp_path, manifest["run_id"], selected, snapshots)
    assert rebuilt["status"] == "pass"
    assert rebuilt["uses_parsed_payload_from_raw"] is True
    assert captured["normalize"]
    assert captured["validate"]
    book_v = verify_http_evidence_file(tmp_path, tmp_path / signal["orderbook_evidence_path"])
    gamma_v = verify_http_evidence_file(tmp_path, tmp_path / signal["gamma_evidence_path"])
    assert captured["normalize"][-1] == book_v["parsed_payload_from_raw"]
    assert captured["validate"][-1][0] == gamma_v["parsed_payload_from_raw"]

    check = sim.verify_live_readonly_evidence(tmp_path, manifest, rebuilt)
    assert check["ok"]
    assert check["raw_payload_binding_result"] == "pass"


def test_release_blocked_when_metadata_payload_rewritten_with_hash(tmp_path):
    from src.forward_reporting_v5_1_8 import compute_release_status
    from src.polymarket_public_adapter_v5_1_8 import content_hash, write_json

    manifest, signal, _ = _run_fixture_live(tmp_path)
    out_dir = tmp_path / "data/forward_v5_1_8/live_integration" / manifest["run_id"]
    meta_path = next((out_dir / "raw_orderbooks").glob("*_orderbook.json"))
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    forged = {"asks": [{"price": "0.99", "size": "1"}], "bids": [], "tampered": True}
    meta["payload"] = forged
    meta["payload_sha256"] = content_hash(forged)
    write_json(meta_path, meta)
    release = compute_release_status(
        manifest,
        signal,
        {"status": "pass"},
        {"ok": True},
        {"ok": True},
        {"ok": True, "formal_started_at_utc": None},
        30,
        root=tmp_path,
    )
    assert release["release_status"] == "BLOCKED_PENDING_LIVE_EVIDENCE"
    assert release["raw_payload_binding_result"] == "fail"
    assert any("binding" in r or "payload" in r for r in release["blocked_reasons"])
