import csv
import json
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.forward_simulation_v5_1_1 import (
    DEMO,
    FORMAL,
    STRATEGY_IDS,
    LedgerLock,
    aggregate_results,
    audit_integrity,
    connect,
    db_path,
    init_ledger,
    load_config,
    make_event_key,
    pause_resume_stop,
    process_entry,
    process_entry_batch,
    process_exit,
    process_exit_batch,
    register_signals,
    run_loop,
    settle,
    signal_position,
    start_formal,
    status,
    write_settlement_file,
    write_signal_template,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_CONFIG = PROJECT_ROOT / "config/forward_simulation_v5_1_1.yaml"
FREEZE = datetime(2026, 7, 21, 0, 0, tzinfo=timezone.utc)


def copy_rc_root(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "rcroot"
    for rel in [
        "src/forward_simulation_v5_1_1.py",
        "src/forward_reporting_v5_1_1.py",
        "config/forward_simulation_v5_1_1.yaml",
        "schemas/forward_simulation_v5_1_1.sql",
        "reports/FORWARD_SIMULATION_V5_1_PREREGISTRATION.md",
    ]:
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PROJECT_ROOT / rel, dst)
    return root, root / "config/forward_simulation_v5_1_1.yaml"


def signal_row(signal_id="sig1", token_id="tok1", created=None, city="City", date="2099-01-01", metric="high", intended="100", max_price="0.10", extra=None):
    created = created or FREEZE
    row = {
        "signal_id": signal_id,
        "created_at_utc": created.isoformat(),
        "city": city,
        "weather_date_local": date,
        "weather_metric": metric,
        "market_slug": f"{city.lower()}-{date}-{metric}",
        "condition_id": f"cond-{token_id}",
        "token_id": token_id,
        "outcome": "YES",
        "side": "BUY",
        "forecast_temperature": "30",
        "forecast_probability": "0.62",
        "market_probability_at_signal": "0.10",
        "intended_usd": intended,
        "max_entry_price": max_price,
        "source": "pytest_fixture",
        "notes": "",
    }
    if extra:
        row.update(extra)
    return row


def write_signals(path: Path, rows):
    fields = list({k for row in rows for k in row} | set(signal_row()))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def register(root: Path, rows, mode=DEMO, now=FREEZE):
    sig_file = root / "signals_fixture.csv"
    write_signals(sig_file, rows)
    return register_signals(root, mode, PROJECT_CONFIG if mode == DEMO else root / "config/forward_simulation_v5_1_1.yaml", sig_file, now=now)


def entry_book(ts="entry", price="0.10", size="1000"):
    return {"timestamp": ts, "bids": [{"price": "0.09", "size": "1000"}], "asks": [{"price": price, "size": size}]}


def exit_book(ts="exit", price="0.25", size="1000"):
    return {"timestamp": ts, "bids": [{"price": price, "size": size}], "asks": [{"price": "0.26", "size": "1000"}]}


def settlement_rows(signal_ids, value="0", observed=None):
    observed = observed or (FREEZE + timedelta(days=2))
    rows = []
    for sid in signal_ids:
        raw = json.dumps({"signal_id": sid, "result": value}, sort_keys=True)
        rows.append({
            "signal_id": sid,
            "condition_id": "cond-tok1",
            "token_id": "tok1",
            "source_type": "fixture",
            "source": "pytest",
            "source_reference": f"fixture://{sid}",
            "observed_at_utc": observed.isoformat(),
            "raw_response": raw,
            "evidence_hash": __import__("hashlib").sha256(raw.encode()).hexdigest(),
            "settlement_outcome": "YES" if value == "1" else "NO",
            "settlement_value": value,
            "operator_notes": "pytest",
        })
    return rows


def settle_file(root: Path, rows):
    path = root / "settlements_fixture.csv"
    write_settlement_file(path, rows)
    return path


def table_count(root: Path, mode: str, table: str, cfg=PROJECT_CONFIG):
    conf = load_config(cfg)
    conn = connect(db_path(root, mode, conf))
    try:
        return conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
    finally:
        conn.close()


def test_shared_entry_depth_same_token_same_snapshot_is_not_reused(tmp_path):
    root = tmp_path
    register(root, [signal_row("a", "tok"), signal_row("b", "tok", created=FREEZE + timedelta(seconds=1))])
    process_entry_batch(root, DEMO, PROJECT_CONFIG, ["b", "a"], {"tok": entry_book(size="1000")}, now=FREEZE + timedelta(seconds=2))
    conf = load_config(PROJECT_CONFIG)
    conn = connect(db_path(root, DEMO, conf))
    try:
        total = conn.execute("SELECT SUM(filled_shares) v FROM entry_fills").fetchone()["v"]
        assert total == pytest.approx(1000)
        per_strategy = conn.execute("SELECT strategy_id, SUM(entry_shares) v FROM strategy_lots GROUP BY strategy_id").fetchall()
        assert {r["strategy_id"]: r["v"] for r in per_strategy} == {strategy: pytest.approx(1000) for strategy in STRATEGY_IDS}
    finally:
        conn.close()


def test_shared_exit_depth_same_strategy_three_triggers_is_not_reused(tmp_path):
    root = tmp_path
    rows = [signal_row(f"s{i}", "tok", created=FREEZE + timedelta(seconds=i), intended="100") for i in range(3)]
    register(root, rows)
    process_entry_batch(root, DEMO, PROJECT_CONFIG, [r["signal_id"] for r in rows], {"tok": entry_book(size="3000")}, now=FREEZE + timedelta(seconds=10))
    process_exit_batch(root, DEMO, PROJECT_CONFIG, [r["signal_id"] for r in rows], {"tok": exit_book(size="500")}, now=FREEZE + timedelta(seconds=20))
    conf = load_config(PROJECT_CONFIG)
    conn = connect(db_path(root, DEMO, conf))
    try:
        for strategy_id in ["tp_2x_sell_50pct", "tp_2x_sell_75pct"]:
            sold = conn.execute("SELECT COALESCE(SUM(filled_shares),0) v FROM exit_fills WHERE strategy_id=?", (strategy_id,)).fetchone()["v"]
            assert sold <= 500 + 1e-6
        assert conn.execute("SELECT COALESCE(SUM(filled_shares),0) v FROM exit_fills WHERE strategy_id='tp_2x_sell_50pct'").fetchone()["v"] == pytest.approx(500)
    finally:
        conn.close()


def test_signal_input_order_does_not_change_stable_depth_allocation(tmp_path):
    outcomes = []
    for root in [tmp_path / "one", tmp_path / "two"]:
        rows = [signal_row("a", "tok", created=FREEZE), signal_row("b", "tok", created=FREEZE + timedelta(seconds=1))]
        register(root, rows)
        order = ["a", "b"] if root.name == "one" else ["b", "a"]
        process_entry_batch(root, DEMO, PROJECT_CONFIG, order, {"tok": entry_book(size="1000")}, now=FREEZE + timedelta(seconds=2))
        conf = load_config(PROJECT_CONFIG)
        conn = connect(db_path(root, DEMO, conf))
        try:
            outcomes.append({r["signal_id"]: r["filled_shares"] for r in conn.execute("SELECT signal_id, filled_shares FROM entry_fills")})
        finally:
            conn.close()
    assert outcomes[0] == outcomes[1] == {"a": 1000.0}


def test_strategy_branches_independently_replay_orderbook_but_not_within_branch(tmp_path):
    root = tmp_path
    register(root, [signal_row("sig", "tok", intended="100")])
    process_entry(root, DEMO, PROJECT_CONFIG, "sig", entry_book(size="1000"), now=FREEZE + timedelta(seconds=1))
    process_exit(root, DEMO, PROJECT_CONFIG, "sig", exit_book(price="0.60", size="500"), now=FREEZE + timedelta(seconds=2))
    conf = load_config(PROJECT_CONFIG)
    conn = connect(db_path(root, DEMO, conf))
    try:
        sold = {r["strategy_id"]: r["v"] for r in conn.execute("SELECT strategy_id, SUM(filled_shares) v FROM exit_fills GROUP BY strategy_id")}
        assert sold["tp_2x_sell_50pct"] == pytest.approx(500)
        assert sold["tp_2x_sell_75pct"] == pytest.approx(500)
        assert sold["tp_5x_sell_25pct"] == pytest.approx(250)
    finally:
        conn.close()


def test_entry_fill_mid_transaction_failure_rolls_back(tmp_path):
    root = tmp_path
    register(root, [signal_row("sig", "tok")])
    with pytest.raises(RuntimeError, match="failpoint:after_entry_fill"):
        process_entry(root, DEMO, PROJECT_CONFIG, "sig", entry_book(), now=FREEZE + timedelta(seconds=1), failpoints={"after_entry_fill": True})
    assert table_count(root, DEMO, "entry_fills") == 0
    assert table_count(root, DEMO, "strategy_lots") == 0
    assert audit_integrity(root, DEMO, PROJECT_CONFIG)["ok"]


def test_exit_fill_mid_transaction_failure_rolls_back(tmp_path):
    root = tmp_path
    register(root, [signal_row("sig", "tok")])
    process_entry(root, DEMO, PROJECT_CONFIG, "sig", entry_book(), now=FREEZE + timedelta(seconds=1))
    with pytest.raises(RuntimeError, match="failpoint:after_exit_fill"):
        process_exit(root, DEMO, PROJECT_CONFIG, "sig", exit_book(), now=FREEZE + timedelta(seconds=2), failpoints={"after_exit_fill": True})
    assert table_count(root, DEMO, "exit_fills") == 0
    assert table_count(root, DEMO, "strategy_triggers") == 0
    assert audit_integrity(root, DEMO, PROJECT_CONFIG)["ok"]


def test_settlement_mid_transaction_failure_rolls_back(tmp_path):
    root = tmp_path
    register(root, [signal_row("sig", "tok")])
    process_entry(root, DEMO, PROJECT_CONFIG, "sig", entry_book(), now=FREEZE + timedelta(seconds=1))
    path = settle_file(root, settlement_rows(["sig"]))
    with pytest.raises(RuntimeError, match="failpoint:after_settlement"):
        settle(root, DEMO, PROJECT_CONFIG, path, now=FREEZE + timedelta(days=2), failpoints={"after_settlement": True})
    assert table_count(root, DEMO, "settlements") == 0
    assert table_count(root, DEMO, "settlement_allocations") == 0
    assert audit_integrity(root, DEMO, PROJECT_CONFIG)["ok"]


def test_state_update_failure_rolls_back(tmp_path):
    root = tmp_path
    init_ledger(root, DEMO, PROJECT_CONFIG)
    with pytest.raises(RuntimeError, match="failpoint:before_state_update"):
        pause_resume_stop(root, DEMO, PROJECT_CONFIG, "pause", failpoints={"before_state_update": True})
    assert status(root, DEMO, PROJECT_CONFIG)["paused"] is False


def test_reporting_code_hash_drift_rejects_formal_write(tmp_path):
    root, cfg = copy_rc_root(tmp_path)
    start_formal(root, cfg, True, now=FREEZE)
    (root / "src/forward_reporting_v5_1_1.py").write_text("drift", encoding="utf-8")
    sig_file = root / "sig.csv"
    write_signals(sig_file, [signal_row("sig", "tok", created=FREEZE + timedelta(seconds=1))])
    with pytest.raises(RuntimeError, match="hash freeze mismatch"):
        register_signals(root, FORMAL, cfg, sig_file, now=FREEZE + timedelta(seconds=2))


def test_schema_hash_drift_rejects_formal_write(tmp_path):
    root, cfg = copy_rc_root(tmp_path)
    start_formal(root, cfg, True, now=FREEZE)
    (root / "schemas/forward_simulation_v5_1_1.sql").write_text("-- drift", encoding="utf-8")
    with pytest.raises(RuntimeError, match="hash freeze mismatch"):
        pause_resume_stop(root, FORMAL, cfg, "pause")


def test_missing_or_wrong_config_never_falls_back(tmp_path):
    root, _ = copy_rc_root(tmp_path)
    with pytest.raises(FileNotFoundError):
        status(root, FORMAL, root / "config/missing.yaml")
    with pytest.raises(FileNotFoundError):
        status(root, FORMAL, tmp_path / "wrong.yaml")


def test_hash_restore_allows_fixture_formal_run(tmp_path):
    root, cfg = copy_rc_root(tmp_path)
    original = (root / "src/forward_reporting_v5_1_1.py").read_text(encoding="utf-8")
    start_formal(root, cfg, True, now=FREEZE)
    (root / "src/forward_reporting_v5_1_1.py").write_text("drift", encoding="utf-8")
    with pytest.raises(RuntimeError):
        pause_resume_stop(root, FORMAL, cfg, "pause")
    (root / "src/forward_reporting_v5_1_1.py").write_text(original, encoding="utf-8")
    sig_file = root / "sig.csv"
    write_signals(sig_file, [signal_row("sig", "tok", created=FREEZE + timedelta(seconds=1))])
    assert len(register_signals(root, FORMAL, cfg, sig_file, now=FREEZE + timedelta(seconds=2))) == 1


def test_three_temperature_bins_same_event_count_as_one_when_all_settled(tmp_path):
    root = tmp_path
    rows = [signal_row(f"s{i}", f"tok{i}", intended="10") for i in range(3)]
    register(root, rows)
    for row in rows:
        process_entry(root, DEMO, PROJECT_CONFIG, row["signal_id"], entry_book(row["signal_id"], size="100"), now=FREEZE + timedelta(seconds=1))
    settlement = []
    for row in rows:
        r = settlement_rows([row["signal_id"]])[0]
        r["token_id"] = row["token_id"]
        r["condition_id"] = row["condition_id"]
        settlement.append(r)
    settle(root, DEMO, PROJECT_CONFIG, settle_file(root, settlement), now=FREEZE + timedelta(days=2))
    assert status(root, DEMO, PROJECT_CONFIG)["settled_event_count"] == 1


def test_same_event_with_one_unsettled_bin_does_not_count_settled(tmp_path):
    root = tmp_path
    rows = [signal_row(f"s{i}", f"tok{i}", intended="10") for i in range(3)]
    register(root, rows)
    for row in rows:
        process_entry(root, DEMO, PROJECT_CONFIG, row["signal_id"], entry_book(row["signal_id"], size="100"), now=FREEZE + timedelta(seconds=1))
    settlement = []
    for row in rows[:2]:
        r = settlement_rows([row["signal_id"]])[0]
        r["token_id"] = row["token_id"]
        r["condition_id"] = row["condition_id"]
        settlement.append(r)
    settle(root, DEMO, PROJECT_CONFIG, settle_file(root, settlement), now=FREEZE + timedelta(days=2))
    assert status(root, DEMO, PROJECT_CONFIG)["settled_event_count"] == 0


def test_unfilled_signal_does_not_count_as_traded(tmp_path):
    root = tmp_path
    register(root, [signal_row("sig", "tok")])
    assert status(root, DEMO, PROJECT_CONFIG)["traded_event_count"] == 0


def test_demo_event_never_counts_in_formal_status(tmp_path):
    root, cfg = copy_rc_root(tmp_path)
    register(root, [signal_row("sig", "tok")], DEMO)
    process_entry(root, DEMO, PROJECT_CONFIG, "sig", entry_book(), now=FREEZE + timedelta(seconds=1))
    init_ledger(root, FORMAL, cfg)
    assert status(root, FORMAL, cfg)["traded_event_count"] == 0


def test_high_and_low_are_different_events(tmp_path):
    root = tmp_path
    rows = [signal_row("high", "tokh", metric="high"), signal_row("low", "tokl", metric="low")]
    register(root, rows)
    assert make_event_key("City", "2099-01-01", "high") != make_event_key("City", "2099-01-01", "low")
    for row in rows:
        process_entry(root, DEMO, PROJECT_CONFIG, row["signal_id"], entry_book(row["signal_id"]), now=FREEZE + timedelta(seconds=1))
    settlement = []
    for row in rows:
        r = settlement_rows([row["signal_id"]])[0]
        r["token_id"] = row["token_id"]
        r["condition_id"] = row["condition_id"]
        settlement.append(r)
    settle(root, DEMO, PROJECT_CONFIG, settle_file(root, settlement), now=FREEZE + timedelta(days=2))
    assert status(root, DEMO, PROJECT_CONFIG)["settled_event_count"] == 2


def test_signal_authenticity_rejections(tmp_path):
    root, cfg = copy_rc_root(tmp_path)
    start_formal(root, cfg, True, now=FREEZE)
    cases = [
        signal_row("manual_reg", "tok", created=FREEZE + timedelta(seconds=1), extra={"registered_at_utc": FREEZE.isoformat()}),
        signal_row("notz", "tok", created=FREEZE + timedelta(seconds=1), extra={"created_at_utc": "2026-07-21T00:00:01"}),
        signal_row("before", "tok", created=FREEZE - timedelta(seconds=1)),
        signal_row("stale", "tok", created=FREEZE + timedelta(seconds=1)),
        signal_row("future", "tok", created=FREEZE + timedelta(seconds=31)),
        signal_row("metadata", "tok", created=FREEZE + timedelta(seconds=1), extra={"market_token_id": "different"}),
    ]
    now_values = {
        "stale": FREEZE + timedelta(seconds=302),
        "future": FREEZE,
    }
    for row in cases:
        path = root / f"{row['signal_id']}.csv"
        write_signals(path, [row])
        assert register_signals(root, FORMAL, cfg, path, now=now_values.get(row["signal_id"], FREEZE + timedelta(seconds=2))) == []


def test_duplicate_signal_id_same_content_idempotent_conflict_rejected(tmp_path):
    root = tmp_path
    row = signal_row("sig", "tok")
    assert len(register(root, [row])) == 1
    assert len(register(root, [row])) == 1
    assert table_count(root, DEMO, "signals") == 1
    conflict = signal_row("sig", "tok2")
    assert register(root, [conflict]) == []
    assert table_count(root, DEMO, "signals") == 1


def test_direct_db_signal_insertion_is_detected(tmp_path):
    root = tmp_path
    register(root, [signal_row("sig", "tok")])
    conf = load_config(PROJECT_CONFIG)
    conn = connect(db_path(root, DEMO, conf))
    try:
        row = dict(conn.execute("SELECT * FROM signals LIMIT 1").fetchone())
        row.update({"signal_id": "fake", "registration_audit_id": "manual", "signal_hash": "fakehash"})
        cols = [k for k in row if k != "row_id"]
        conn.execute(f"INSERT INTO signals({','.join(cols)}) VALUES({','.join(['?']*len(cols))})", [row[c] for c in cols])
        conn.commit()
    finally:
        conn.close()
    assert audit_integrity(root, DEMO, PROJECT_CONFIG)["checks"]["unregistered_signal_rows"] >= 1


def test_zero_fill_and_partial_fill_fees(tmp_path):
    root = tmp_path
    register(root, [signal_row("sig", "tok")])
    process_entry(root, DEMO, PROJECT_CONFIG, "sig", entry_book(price="0.11"), now=FREEZE + timedelta(seconds=1))
    assert table_count(root, DEMO, "entry_fills") == 0
    process_entry(root, DEMO, PROJECT_CONFIG, "sig", entry_book(size="100"), now=FREEZE + timedelta(seconds=2))
    conf = load_config(PROJECT_CONFIG)
    conn = connect(db_path(root, DEMO, conf))
    try:
        fill = conn.execute("SELECT * FROM entry_fills").fetchone()
        assert fill["gross_entry_cost"] == pytest.approx(10)
        assert fill["entry_fee"] == pytest.approx(0.01)
    finally:
        conn.close()


def test_multiple_partial_exit_fees_accumulate_and_round(tmp_path):
    root = tmp_path
    register(root, [signal_row("sig", "tok", intended="200")])
    process_entry(root, DEMO, PROJECT_CONFIG, "sig", entry_book(size="2000"), now=FREEZE + timedelta(seconds=1))
    process_exit(root, DEMO, PROJECT_CONFIG, "sig", exit_book("exit1", size="500"), now=FREEZE + timedelta(seconds=2))
    process_exit(root, DEMO, PROJECT_CONFIG, "sig", exit_book("exit2", size="500"), now=FREEZE + timedelta(seconds=3))
    conf = load_config(PROJECT_CONFIG)
    conn = connect(db_path(root, DEMO, conf))
    try:
        fee = conn.execute("SELECT SUM(exit_fee) v FROM exit_fills WHERE strategy_id='tp_2x_sell_50pct'").fetchone()["v"]
        assert fee == pytest.approx(0.25)
        assert all(len(str(r["exit_fee"]).split(".")[-1]) <= 8 for r in conn.execute("SELECT exit_fee FROM exit_fills"))
    finally:
        conn.close()


def test_strategy_event_and_total_pnl_conservation(tmp_path):
    root = tmp_path
    register(root, [signal_row("sig", "tok")])
    process_entry(root, DEMO, PROJECT_CONFIG, "sig", entry_book(), now=FREEZE + timedelta(seconds=1))
    process_exit(root, DEMO, PROJECT_CONFIG, "sig", exit_book(), now=FREEZE + timedelta(seconds=2))
    settle(root, DEMO, PROJECT_CONFIG, settle_file(root, settlement_rows(["sig"])), now=FREEZE + timedelta(days=2))
    rows = aggregate_results(root, DEMO, PROJECT_CONFIG)
    for row in rows:
        assert row["gross_pnl"] - row["total_fees"] == pytest.approx(row["net_pnl"])
    conf = load_config(PROJECT_CONFIG)
    conn = connect(db_path(root, DEMO, conf))
    try:
        entry_fee = conn.execute("SELECT SUM(entry_fee) v FROM entry_fills").fetchone()["v"]
        assert entry_fee == pytest.approx(0.1)
        assert len({r["entry_fee"] for r in conn.execute("SELECT strategy_id, SUM(entry_fee) entry_fee FROM strategy_lots GROUP BY strategy_id")}) == 1
    finally:
        conn.close()


def test_settlement_evidence_idempotent_conflict_invalid_and_no_exit_after(tmp_path):
    root = tmp_path
    register(root, [signal_row("sig", "tok")])
    process_entry(root, DEMO, PROJECT_CONFIG, "sig", entry_book(), now=FREEZE + timedelta(seconds=1))
    path = settle_file(root, settlement_rows(["sig"], value="0"))
    settle(root, DEMO, PROJECT_CONFIG, path, now=FREEZE + timedelta(days=2))
    settle(root, DEMO, PROJECT_CONFIG, path, now=FREEZE + timedelta(days=2, seconds=1))
    assert table_count(root, DEMO, "settlements") == 4
    conflict = settlement_rows(["sig"], value="1")
    with pytest.raises(RuntimeError, match="conflicting"):
        settle(root, DEMO, PROJECT_CONFIG, settle_file(root, conflict), now=FREEZE + timedelta(days=2, seconds=2))
    invalid = settlement_rows(["sig"], value="2")
    with pytest.raises(ValueError, match="between 0 and 1"):
        settle(root, DEMO, PROJECT_CONFIG, settle_file(root, invalid), now=FREEZE + timedelta(days=2, seconds=3))
    process_exit(root, DEMO, PROJECT_CONFIG, "sig", exit_book("after"), now=FREEZE + timedelta(days=3))
    assert table_count(root, DEMO, "exit_fills") == 0


def test_no_final_event_result_before_settlement(tmp_path):
    root = tmp_path
    register(root, [signal_row("sig", "tok")])
    process_entry(root, DEMO, PROJECT_CONFIG, "sig", entry_book(), now=FREEZE + timedelta(seconds=1))
    rows = aggregate_results(root, DEMO, PROJECT_CONFIG)
    hold = [r for r in rows if r["strategy_id"] == "hold_to_settlement"][0]
    assert hold["settled_event_count"] == 0
    assert hold["net_pnl"] is None


def test_run_loop_lock_stale_pause_stop_and_network_recovery(tmp_path):
    root = tmp_path
    init_ledger(root, DEMO, PROJECT_CONFIG)
    conf = load_config(PROJECT_CONFIG)
    with LedgerLock(root, DEMO, conf, stale_seconds=999):
        with pytest.raises(RuntimeError, match="already holds"):
            with LedgerLock(root, DEMO, conf, stale_seconds=999):
                pass
    stale = root / "data/forward_v5_1_1/demo/.run_loop.lock"
    stale.mkdir(parents=True)
    (stale / "created_at_epoch").write_text("1", encoding="utf-8")
    with LedgerLock(root, DEMO, conf, stale_seconds=0):
        assert True
    register(root, [signal_row("sig", "tok")])
    pause_resume_stop(root, DEMO, PROJECT_CONFIG, "pause")
    calls = {"n": 0}

    def provider(token, purpose):
        calls["n"] += 1
        return entry_book()

    run_loop(root, DEMO, PROJECT_CONFIG, provider, iterations=1, now=FREEZE + timedelta(seconds=1))
    assert calls["n"] == 0
    assert table_count(root, DEMO, "orderbook_snapshots") == 0
    pause_resume_stop(root, DEMO, PROJECT_CONFIG, "resume")
    fail_once = {"n": 0}

    def flaky(token, purpose):
        fail_once["n"] += 1
        if fail_once["n"] == 1:
            raise RuntimeError("network down")
        return entry_book(f"{purpose}-{fail_once['n']}")

    run_loop(root, DEMO, PROJECT_CONFIG, flaky, iterations=2, now=FREEZE + timedelta(seconds=2))
    assert table_count(root, DEMO, "entry_fills") == 1
    pause_resume_stop(root, DEMO, PROJECT_CONFIG, "stop")
    assert run_loop(root, DEMO, PROJECT_CONFIG, provider, iterations=1)["iterations_completed"] == 0


def test_one_token_exception_does_not_stop_other_token(tmp_path):
    root = tmp_path
    rows = [signal_row("bad", "bad"), signal_row("good", "good")]
    register(root, rows)

    def provider(token, purpose):
        if token == "bad":
            raise RuntimeError("bad token")
        return entry_book(f"{token}-{purpose}")

    run_loop(root, DEMO, PROJECT_CONFIG, provider, iterations=1, now=FREEZE + timedelta(seconds=1))
    conf = load_config(PROJECT_CONFIG)
    conn = connect(db_path(root, DEMO, conf))
    try:
        filled = {r["signal_id"] for r in conn.execute("SELECT DISTINCT signal_id FROM entry_fills")}
        assert filled == {"good"}
    finally:
        conn.close()


def corrupt_and_check(tmp_path: Path, case: str) -> tuple[bool, str]:
    root = tmp_path / case
    register(root, [signal_row("sig", "tok")])
    process_entry(root, DEMO, PROJECT_CONFIG, "sig", entry_book(), now=FREEZE + timedelta(seconds=1))
    process_exit(root, DEMO, PROJECT_CONFIG, "sig", exit_book(), now=FREEZE + timedelta(seconds=2))
    settle(root, DEMO, PROJECT_CONFIG, settle_file(root, settlement_rows(["sig"])), now=FREEZE + timedelta(days=2))
    conf = load_config(PROJECT_CONFIG)
    mode_for_case = FORMAL if case in {"demo_pollution_formal", "hash_drift", "formal_timeout"} else DEMO
    if mode_for_case == FORMAL:
        init_ledger(root, FORMAL, PROJECT_CONFIG)
        src = connect(db_path(root, DEMO, conf))
        dst = connect(db_path(root, FORMAL, conf))
        try:
            src.backup(dst)
            dst.execute("INSERT OR REPLACE INTO state(key,value) VALUES('mode','formal')")
            dst.commit()
        finally:
            src.close()
            dst.close()
    conn = connect(db_path(root, mode_for_case, conf))
    try:
        if case == "duplicate_signal_id":
            row = dict(conn.execute("SELECT * FROM signals LIMIT 1").fetchone())
            cols = [k for k in row if k != "row_id"]
            conn.execute(f"INSERT INTO signals({','.join(cols)}) VALUES({','.join(['?']*len(cols))})", [row[c] for c in cols])
        elif case == "duplicate_fill_id":
            row = dict(conn.execute("SELECT * FROM entry_fills LIMIT 1").fetchone())
            cols = [k for k in row if k != "row_id"]
            conn.execute(f"INSERT INTO entry_fills({','.join(cols)}) VALUES({','.join(['?']*len(cols))})", [row[c] for c in cols])
        elif case == "duplicate_snapshot_id":
            row = dict(conn.execute("SELECT * FROM orderbook_snapshots LIMIT 1").fetchone())
            cols = [k for k in row if k != "row_id"]
            conn.execute(f"INSERT INTO orderbook_snapshots({','.join(cols)}) VALUES({','.join(['?']*len(cols))})", [row[c] for c in cols])
        elif case in {"negative_inventory", "over_sell"}:
            lot = conn.execute("SELECT * FROM strategy_lots WHERE strategy_id='hold_to_settlement' LIMIT 1").fetchone()
            conn.execute("INSERT INTO settlement_allocations(settlement_allocation_id,settlement_id,strategy_id,signal_id,event_key,token_id,lot_id,settled_shares,settlement_proceeds,settlement_fee,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?)", ("bad", "bad", lot["strategy_id"], lot["signal_id"], lot["event_key"], lot["token_id"], lot["lot_id"], 999999, 0, 0, DEMO))
        elif case == "trigger_overfill":
            trig = dict(conn.execute("SELECT * FROM strategy_triggers LIMIT 1").fetchone())
            trig["trigger_filled_shares"] = trig["trigger_target_shares"] + 1
            cols = [k for k in trig if k != "row_id"]
            conn.execute(f"INSERT INTO strategy_triggers({','.join(cols)}) VALUES({','.join(['?']*len(cols))})", [trig[c] for c in cols])
        elif case == "strategy_entry_inconsistent":
            conn.execute("DELETE FROM strategy_lots WHERE strategy_id='tp_5x_sell_25pct'")
        elif case == "signal_event_sum_inconsistent":
            conn.execute("UPDATE event_results SET total_fees=total_fees+1")
        elif case == "demo_pollution_formal":
            conn.execute("UPDATE signals SET mode='demo'")
        elif case == "hash_drift":
            conn.execute("INSERT OR REPLACE INTO state(key,value) VALUES('formal_started_at_utc',?)", (FREEZE.isoformat(),))
            conn.execute("INSERT OR REPLACE INTO state(key,value) VALUES('core_code_sha256','bad')")
        elif case == "settled_after_exit":
            conn.execute("UPDATE exit_fills SET filled_at_utc=?", ((FREEZE + timedelta(days=3)).isoformat(),))
        elif case == "timestamp_order":
            conn.execute("UPDATE entry_fills SET filled_at_utc=?", ((FREEZE - timedelta(days=1)).isoformat(),))
        elif case == "formal_timeout":
            conn.execute("UPDATE signals SET mode='formal', registered_at_utc=?", ((FREEZE + timedelta(seconds=400)).isoformat(),))
        elif case == "missing_settlement_evidence":
            conn.execute("UPDATE settlements SET raw_response=''")
        elif case == "evidence_hash_mismatch":
            conn.execute("UPDATE settlements SET raw_response='tampered'")
        elif case == "duplicate_settlement_result":
            row = dict(conn.execute("SELECT * FROM settlements LIMIT 1").fetchone())
            cols = [k for k in row if k != "row_id"]
            conn.execute(f"INSERT INTO settlements({','.join(cols)}) VALUES({','.join(['?']*len(cols))})", [row[c] for c in cols])
        elif case == "repeated_orderbook_depth":
            fill = dict(conn.execute("SELECT * FROM exit_fills WHERE strategy_id='tp_2x_sell_50pct' LIMIT 1").fetchone())
            fill["filled_shares"] = 999999
            cols = [k for k in fill if k != "row_id"]
            conn.execute(f"INSERT INTO exit_fills({','.join(cols)}) VALUES({','.join(['?']*len(cols))})", [fill[c] for c in cols])
        conn.commit()
    finally:
        conn.close()
    result = audit_integrity(root, mode_for_case, PROJECT_CONFIG)
    return (not result["ok"], ",".join(k for k, v in result["checks"].items() if isinstance(v, (int, float)) and v))


def test_integrity_negative_tests_detect_all_17_corruptions(tmp_path):
    cases = [
        "duplicate_signal_id",
        "duplicate_fill_id",
        "duplicate_snapshot_id",
        "negative_inventory",
        "over_sell",
        "trigger_overfill",
        "strategy_entry_inconsistent",
        "signal_event_sum_inconsistent",
        "demo_pollution_formal",
        "hash_drift",
        "settled_after_exit",
        "timestamp_order",
        "formal_timeout",
        "missing_settlement_evidence",
        "evidence_hash_mismatch",
        "duplicate_settlement_result",
        "repeated_orderbook_depth",
    ]
    results = {case: corrupt_and_check(tmp_path, case) for case in cases}
    assert all(detected for detected, _ in results.values()), results
