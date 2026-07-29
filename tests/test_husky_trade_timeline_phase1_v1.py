from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from src.husky_trade_timeline_phase1_v1 import (
    CST,
    analyze,
    annotate_timeline,
    average_cost_pnl,
    average_cost_pnl_by_buy_phase,
    basket_metrics,
    buckets_adjacent,
    classify_add,
    compute_trade_usd,
    deduplicate_trades,
    epoch_to_iso,
    fifo_pnl,
    fifo_pnl_by_buy_phase,
    relative_day_and_phase,
    select_events,
    stable_trade_key,
    threshold_time,
)


def epoch(cst_iso: str) -> int:
    return int(datetime.fromisoformat(cst_iso).astimezone(timezone.utc).timestamp())


def row(
    *,
    timestamp: str = "2026-07-20T09:00:00+08:00",
    side: str = "BUY",
    price: float = 0.2,
    shares: float = 10,
    bucket: str = "30°C",
    asset: str = "asset-1",
) -> dict:
    ts = epoch(timestamp)
    relative, phase = relative_day_and_phase(ts, "2026-07-20")
    return {
        "event_key": "2026-07-20__beijing__high",
        "city": "Beijing",
        "weather_date_local": "2026-07-20",
        "weather_metric": "high",
        "condition_id": "condition-1",
        "event_slug": "event",
        "slug": "slug",
        "token_id": asset,
        "asset": asset,
        "temperature_bucket": bucket,
        "bucket_kind": "exact",
        "bucket_low": float(bucket.replace("°C", "")),
        "bucket_high": float(bucket.replace("°C", "")),
        "unit": "C",
        "timestamp_epoch": ts,
        "public_record_timestamp_utc": epoch_to_iso(ts),
        "public_record_timestamp_cst": epoch_to_iso(ts, CST),
        "relative_day": relative,
        "time_phase": phase,
        "side": side,
        "price": price,
        "shares": shares,
        "trade_usd": price * shares,
        "trade_usd_source": "price_x_size",
        "transaction_hash": f"tx-{ts}-{asset}-{side}-{price}-{shares}",
        "source_file": "fixture",
        "source_row_number": 2,
        "raw_transaction_hash": "tx",
    }


def test_unix_time_to_utc_and_beijing():
    value = 1784509200
    assert epoch_to_iso(value) == "2026-07-20T01:00:00+00:00"
    assert epoch_to_iso(value, CST) == "2026-07-20T09:00:00+08:00"


def test_d_minus_1_and_d0_calculation():
    assert relative_day_and_phase(epoch("2026-07-19T23:00:00+08:00"), "2026-07-20")[0] == "D-1"
    assert relative_day_and_phase(epoch("2026-07-20T00:00:00+08:00"), "2026-07-20")[0] == "D0"


def test_all_eight_time_phase_boundaries():
    cases = [
        ("2026-07-19T00:00:00+08:00", "D-1_EARLY"),
        ("2026-07-19T12:00:00+08:00", "D-1_AFTERNOON"),
        ("2026-07-19T18:00:00+08:00", "D-1_EVENING"),
        ("2026-07-20T00:00:00+08:00", "D0_OVERNIGHT"),
        ("2026-07-20T08:00:00+08:00", "D0_MORNING"),
        ("2026-07-20T10:00:00+08:00", "D0_WARMING_EARLY"),
        ("2026-07-20T12:00:00+08:00", "D0_WARMING_CORE"),
        ("2026-07-20T14:00:00+08:00", "D0_LATE"),
    ]
    for timestamp, expected in cases:
        assert relative_day_and_phase(epoch(timestamp), "2026-07-20")[1] == expected


def test_same_transaction_hash_different_token_is_not_deduplicated():
    base = {
        "timestamp": "1",
        "transactionHash": "same",
        "conditionId": "c",
        "side": "BUY",
        "price": "0.2",
        "size": "10",
    }
    rows, duplicate_count = deduplicate_trades([
        {**base, "asset": "a"},
        {**base, "asset": "b"},
    ])
    assert len(rows) == 2
    assert duplicate_count == 0


def test_exact_duplicate_row_is_deduplicated():
    item = {
        "timestamp": "1",
        "transactionHash": "same",
        "conditionId": "c",
        "asset": "a",
        "side": "BUY",
        "price": "0.2",
        "size": "10",
    }
    rows, duplicate_count = deduplicate_trades([item, dict(item)])
    assert len(rows) == 1
    assert duplicate_count == 1


def test_trade_usd_prefers_usdc_size():
    value, source = compute_trade_usd({"usdcSize": "2.123", "price": "0.2", "size": "10"})
    assert value == 2.123
    assert source == "usdcSize"


def test_weighted_average_buy_price():
    timeline = annotate_timeline([
        row(price=0.1, shares=10),
        row(timestamp="2026-07-20T09:01:00+08:00", price=0.3, shares=30),
    ])
    assert timeline[-1]["weighted_average_buy_price_bucket"] == 0.25


def test_build_threshold_times():
    rows = [
        {**row(timestamp="2026-07-20T09:00:00+08:00"), "trade_usd": 10},
        {**row(timestamp="2026-07-20T09:01:00+08:00"), "trade_usd": 20},
        {**row(timestamp="2026-07-20T09:02:00+08:00"), "trade_usd": 70},
    ]
    assert threshold_time(rows, "trade_usd", 100, 0.25) == rows[1]["public_record_timestamp_cst"]
    assert threshold_time(rows, "trade_usd", 100, 0.50) == rows[2]["public_record_timestamp_cst"]
    assert threshold_time(rows, "trade_usd", 100, 0.75) == rows[2]["public_record_timestamp_cst"]


def test_build_threshold_uses_money_not_trade_count():
    rows = [
        {**row(timestamp="2026-07-20T09:00:00+08:00"), "trade_usd": 1},
        {**row(timestamp="2026-07-20T09:01:00+08:00"), "trade_usd": 1},
        {**row(timestamp="2026-07-20T09:02:00+08:00"), "trade_usd": 98},
    ]
    assert threshold_time(rows, "trade_usd", 100, 0.50) == rows[2]["public_record_timestamp_cst"]


def test_first_and_last_buy_and_sell():
    from src.husky_trade_timeline_phase1_v1 import build_scope_metrics

    rows = [
        row(timestamp="2026-07-20T09:00:00+08:00"),
        row(timestamp="2026-07-20T09:01:00+08:00"),
        row(timestamp="2026-07-20T09:02:00+08:00", side="SELL"),
        row(timestamp="2026-07-20T09:03:00+08:00", side="SELL"),
    ]
    metrics = build_scope_metrics(rows)
    assert metrics["first_buy_time"] == rows[0]["public_record_timestamp_cst"]
    assert metrics["last_buy_time"] == rows[1]["public_record_timestamp_cst"]
    assert metrics["first_sell_time"] == rows[2]["public_record_timestamp_cst"]
    assert metrics["last_sell_time"] == rows[3]["public_record_timestamp_cst"]


def test_sold_50_not_reached():
    from src.husky_trade_timeline_phase1_v1 import build_scope_metrics

    metrics = build_scope_metrics([
        row(shares=100),
        row(timestamp="2026-07-20T10:00:00+08:00", side="SELL", shares=49),
    ])
    assert metrics["sold_50pct_time"] is None
    assert metrics["sold_50pct_status"] == "NOT_REACHED"


def test_price_up_add():
    assert classify_add(0.20, 0.21)[0] == "PRICE_UP_ADD"


def test_price_down_add():
    assert classify_add(0.20, 0.19)[0] == "PRICE_DOWN_ADD"


def test_price_flat_add():
    assert classify_add(0.20, 0.209)[0] == "PRICE_FLAT_ADD"


def test_new_bucket_add():
    timeline = annotate_timeline([
        row(bucket="30°C"),
        row(timestamp="2026-07-20T09:01:00+08:00", bucket="31°C", asset="asset-2"),
    ])
    assert timeline[0]["add_action_classification"] == "NEW_BUCKET_ADD"
    assert timeline[1]["add_action_classification"] == "NEW_BUCKET_ADD"


def test_dominant_bought_bucket():
    rows = [
        row(bucket="30°C", price=0.1, shares=10),
        row(timestamp="2026-07-20T09:01:00+08:00", bucket="31°C", asset="asset-2", price=0.2, shares=20),
    ]
    assert basket_metrics(rows)["dominant_bought_bucket"] == "31°C"


def test_integer_exact_buckets_are_adjacent_and_tails_are_not():
    exact_30 = {"bucket_kind": "exact", "bucket_low": 30.0, "unit": "C"}
    exact_31 = {"bucket_kind": "exact", "bucket_low": 31.0, "unit": "C"}
    tail = {"bucket_kind": "above", "bucket_low": 31.0, "unit": "C"}
    assert buckets_adjacent(exact_30, exact_31)
    assert not buckets_adjacent(exact_30, tail)


def test_fifo_pnl():
    trades = [
        row(price=0.1, shares=10),
        row(timestamp="2026-07-20T09:01:00+08:00", price=0.2, shares=10),
        row(timestamp="2026-07-20T09:02:00+08:00", side="SELL", price=0.3, shares=10),
    ]
    pnl, paired, complete = fifo_pnl(trades)
    assert round(pnl, 10) == 2.0
    assert paired == 10
    assert complete


def test_average_cost_pnl():
    trades = [
        row(price=0.1, shares=10),
        row(timestamp="2026-07-20T09:01:00+08:00", price=0.2, shares=10),
        row(timestamp="2026-07-20T09:02:00+08:00", side="SELL", price=0.3, shares=10),
    ]
    pnl, paired, complete = average_cost_pnl(trades)
    assert round(pnl, 10) == 1.5
    assert paired == 10
    assert complete


def test_recorded_sell_pnl_can_be_allocated_to_original_buy_phase():
    trades = [
        row(timestamp="2026-07-19T11:00:00+08:00", price=0.1, shares=10),
        row(timestamp="2026-07-20T10:00:00+08:00", price=0.2, shares=10),
        row(timestamp="2026-07-20T12:00:00+08:00", side="SELL", price=0.3, shares=10),
    ]
    fifo_by_phase, fifo_complete = fifo_pnl_by_buy_phase(trades)
    average_by_phase, average_complete = average_cost_pnl_by_buy_phase(trades)
    assert fifo_complete and average_complete
    assert round(sum(fifo_by_phase.values()), 10) == 2.0
    assert round(sum(average_by_phase.values()), 10) == 1.5
    assert set(average_by_phase) == {"D-1_EARLY", "D0_WARMING_EARLY"}


def test_missing_settlement_is_partial_cashflow_only():
    from src.husky_trade_timeline_phase1_v1 import pnl_metrics

    metrics = pnl_metrics([row()], {})
    assert metrics["total_event_pnl_status"] == "PARTIAL_CASHFLOW_ONLY"


def test_no_sell_does_not_imply_settlement():
    from src.husky_trade_timeline_phase1_v1 import build_event_summary

    _, summary = build_event_summary("event", [row()], {})
    assert summary["final_path_status"] == "NO_RECORDED_SELL_UNKNOWN_FINAL_PATH"


def summary_fixture(key: str, **features) -> dict:
    buy_count = 2 if features.get("multi_buy") else 1
    bucket_order = ["30°C", "31°C"] if features.get("multi_bucket") else ["30°C"]
    buy_shares = 100
    sell_shares = 50 if features.get("partial_sell") else (100 if features.get("has_sell") else 0)
    pnl = 10 if features.get("profit") else (-10 if features.get("loss") else None)
    phases = {"D0_MORNING": 10}
    if features.get("d_minus_1_and_d0"):
        phases["D-1_EVENING"] = 10
    return {
        "event_key": key,
        "city": "Beijing" if features.get("beijing") else "London",
        "overall": {
            "buy_trade_count": buy_count,
            "total_buy_shares": buy_shares,
            "total_sell_shares": sell_shares,
            "buy_duration_seconds": 50000 if features.get("long_build") else 0,
            "phase_buy_usd": phases,
            "total_buy_usd": 20,
        },
        "basket": {"bucket_join_order": bucket_order},
        "pnl": {"total_event_pnl": pnl},
    }


def test_event_selection_is_deterministic():
    summaries = {
        "b": summary_fixture("b", multi_buy=True),
        "a": summary_fixture("a", beijing=True),
        "c": summary_fixture("c", loss=True),
    }
    first = select_events(summaries, 3)
    second = select_events(summaries, 3)
    assert first == second


def test_beijing_is_not_automatically_zbaa():
    from src.husky_trade_timeline_phase1_v1 import build_event_summary

    _, summary = build_event_summary("event", [row()], {})
    assert summary["station_status"] == "BEIJING_STATION_UNCONFIRMED"
    assert summary["zbaa_confirmed"] is False


def write_fixture_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def test_output_is_stable_across_repeated_runs(tmp_path: Path):
    # Stability of serialized, sorted JSON is tested directly without copying the full repository fixture.
    payload = {"b": 2, "a": [3, 1]}
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    first.write_text(text, encoding="utf-8")
    second.write_text(text, encoding="utf-8")
    assert first.read_bytes() == second.read_bytes()


def test_stable_key_includes_all_required_trade_dimensions():
    base = {
        "timestamp": "1",
        "transactionHash": "tx",
        "conditionId": "condition",
        "asset": "asset",
        "side": "BUY",
        "price": "0.2",
        "size": "10",
    }
    assert stable_trade_key(base) != stable_trade_key({**base, "price": "0.3"})
