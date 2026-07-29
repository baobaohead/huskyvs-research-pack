from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.husky_beijing_full_trade_study_v1 import (
    HUSKY_WALLET,
    Window,
    activity_join_key,
    annotate_adds,
    archetype_labels,
    bucket_metrics,
    buckets_adjacent,
    candidate_rows,
    checkpoint_epoch,
    classify_average_cost_add,
    classify_entry,
    classify_exit,
    classify_price_add,
    deduplicate_records,
    event_key,
    event_summary,
    fetch_activity_window,
    half_hour_bin,
    is_beijing_highest_market,
    merge_public_fills,
    parse_bucket,
    parse_weather_date,
    relative_phase,
    stable_trade_key,
    threshold_timestamp,
)


def epoch(value: str) -> int:
    return int(datetime.fromisoformat(value).astimezone(timezone.utc).timestamp())


def raw(
    *,
    event_slug: str = "highest-temperature-in-beijing-on-july-20-2026",
    slug: str = "highest-temperature-in-beijing-on-july-20-2026-30c",
    title: str = "Will the highest temperature in Beijing be 30°C on July 20?",
    city: str = "Beijing",
    metric: str = "high",
    wallet: str = HUSKY_WALLET,
    timestamp: int | None = None,
    side: str = "BUY",
    price: float = 0.2,
    size: float = 10,
    asset: str = "asset-30",
    transaction_hash: str = "tx-1",
) -> dict:
    return {
        "proxyWallet": wallet,
        "timestamp": timestamp or epoch("2026-07-20T09:00:00+08:00"),
        "conditionId": "condition-1",
        "asset": asset,
        "side": side,
        "price": price,
        "size": size,
        "transactionHash": transaction_hash,
        "eventSlug": event_slug,
        "slug": slug,
        "title": title,
        "outcome": "Yes",
        "city": city,
        "weather_metric": metric,
    }


def normalized_fill(
    *,
    timestamp: str = "2026-07-20T09:00:00+08:00",
    side: str = "BUY",
    price: float = 0.2,
    shares: float = 10,
    usd: float | None = None,
    bucket: str = "30°C",
    asset: str = "asset-30",
    bucket_kind: str = "exact",
    bucket_low: float | None = 30,
) -> dict:
    ts = epoch(timestamp)
    return {
        "event_key": "2026-07-20__beijing__high",
        "weather_date": "2026-07-20",
        "city": "Beijing",
        "weather_metric": "high",
        "station_status": "BEIJING_STATION_UNCONFIRMED",
        "condition_id": f"condition-{asset}",
        "event_slug": "highest-temperature-in-beijing-on-july-20-2026",
        "slug": f"highest-temperature-in-beijing-on-july-20-2026-{bucket}",
        "asset": asset,
        "outcome": "Yes",
        "temperature_bucket": bucket,
        "bucket_kind": bucket_kind,
        "bucket_low": bucket_low,
        "bucket_high": bucket_low,
        "unit": "C",
        "timestamp_epoch": ts,
        "public_trade_time_utc": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
        "public_trade_time_cst": timestamp,
        "relative_phase": relative_phase(ts, "2026-07-20"),
        "half_hour_bin": half_hour_bin(ts),
        "side": side,
        "price": price,
        "shares": shares,
        "trade_usd": usd if usd is not None else price * shares,
        "trade_usd_source": "price_x_size",
        "transaction_hash": f"{asset}-{side}-{ts}-{price}-{shares}",
        "source_repository_trade": True,
        "source_repository_activity": True,
        "source_current_public_api": False,
        "source_row_number": "fixture",
        "activity_match_status": "EXACT_SIZE_MATCH",
        "previous_same_bucket_buy_price": None,
        "price_change_vs_previous_buy": None,
        "price_add_class": "",
        "pretrade_average_cost": None,
        "price_change_vs_average_cost": None,
        "average_cost_add_class": "",
    }


def test_only_beijing_highest_market_is_included():
    assert is_beijing_highest_market(raw())


def test_other_beijing_topic_is_excluded():
    assert not is_beijing_highest_market(raw(
        event_slug="will-it-rain-in-beijing",
        slug="will-it-rain-in-beijing",
        title="Will it rain in Beijing?",
    ))


def test_other_city_is_excluded():
    assert not is_beijing_highest_market(raw(
        event_slug="highest-temperature-in-london-on-july-20-2026",
        slug="highest-temperature-in-london-on-july-20-2026-30c",
        title="Will the highest temperature in London be 30°C on July 20?",
        city="London",
    ))


def test_wrong_wallet_is_excluded():
    assert not is_beijing_highest_market(raw(wallet="0x" + "1" * 40))


def test_non_high_metric_is_excluded():
    assert not is_beijing_highest_market(raw(metric="low"))


def test_weather_date_from_event_slug():
    assert parse_weather_date(raw()) == "2026-07-20"


def test_beijing_event_key():
    assert event_key(raw()) == "2026-07-20__beijing__high"


def test_composite_key_deduplicates_exact_fill():
    rows, duplicates = deduplicate_records([raw(), raw()])
    assert len(rows) == 1 and duplicates == 1


def test_same_hash_different_token_is_not_deleted():
    rows, duplicates = deduplicate_records([raw(asset="a"), raw(asset="b")])
    assert len(rows) == 2 and duplicates == 0


def test_same_hash_different_size_is_not_deleted():
    rows, _ = deduplicate_records([raw(size=10), raw(size=11)])
    assert len(rows) == 2


def test_activity_join_ignores_size_only():
    assert activity_join_key(raw(size=10)) == activity_join_key(raw(size=11))


class FakeClient:
    def __init__(self, pages: list[list[dict]]):
        self.pages = iter(pages)
        self.params = []

    def get_json(self, url, params):
        self.params.append(params)
        return next(self.pages)


def test_activity_window_paginates_offsets():
    client = FakeClient([[raw()] * 2, [raw()]])
    rows = fetch_activity_window(client, HUSKY_WALLET, Window(1, 2), limit=2, offset_cap=10)
    assert len(rows) == 3
    assert [item["offset"] for item in client.params] == [0, 2]


def test_activity_window_splits_near_offset_cap():
    client = FakeClient([[raw()] * 2, [], []])
    rows = fetch_activity_window(client, HUSKY_WALLET, Window(1, 5), limit=2, offset_cap=2)
    assert rows == []
    assert {(item["start"], item["end"]) for item in client.params[1:]} == {(1, 3), (3, 5)}


def test_activity_failure_is_not_silently_skipped():
    class Failing:
        def get_json(self, url, params):
            raise RuntimeError("failure")
    with pytest.raises(RuntimeError):
        fetch_activity_window(Failing(), HUSKY_WALLET, Window(1, 2))


def test_exact_activity_match_is_recorded():
    trades = [raw()]
    activity = [{**raw(), "type": "TRADE", "usdcSize": 2}]
    fills, _ = merge_public_fills(trades, [], [], activity)
    assert fills[0]["activity_match_status"] == "EXACT_SIZE_MATCH"


def test_nearest_activity_match_is_recorded():
    trades = [raw(size=10)]
    activity = [{**raw(size=9), "type": "TRADE", "usdcSize": 2}]
    fills, _ = merge_public_fills(trades, [], [], activity)
    assert fills[0]["activity_match_status"] == "NEAREST_SIZE_MATCH"


def test_missing_activity_match_is_recorded():
    fills, _ = merge_public_fills([raw()], [], [], [])
    assert fills[0]["activity_match_status"] == "NO_ACTIVITY_MATCH"


def test_activity_usdc_size_controls_cash_amount():
    activity = [{**raw(), "type": "TRADE", "usdcSize": 9.99}]
    fills, _ = merge_public_fills([raw()], [], [], activity)
    assert fills[0]["trade_usd"] == 9.99


def test_first_and_last_observed_trade_sorting():
    rows = [normalized_fill(timestamp="2026-07-20T10:00:00+08:00"), normalized_fill()]
    assert min(row["timestamp_epoch"] for row in rows) == epoch("2026-07-20T09:00:00+08:00")


def test_thresholds_use_buy_usd_not_fill_count():
    rows = [
        normalized_fill(usd=1),
        normalized_fill(timestamp="2026-07-20T09:01:00+08:00", usd=1),
        normalized_fill(timestamp="2026-07-20T09:02:00+08:00", usd=98),
    ]
    assert threshold_timestamp(rows, "trade_usd", 100, 0.5) == rows[-1]["timestamp_epoch"]


@pytest.mark.parametrize("fraction", [0.10, 0.25, 0.50, 0.75, 0.90])
def test_all_build_thresholds(fraction):
    rows = [normalized_fill(usd=100)]
    assert threshold_timestamp(rows, "trade_usd", 100, fraction) == rows[0]["timestamp_epoch"]


def test_relative_phase_d_minus_2():
    assert relative_phase(epoch("2026-07-18T23:00:00+08:00"), "2026-07-20") == "D-2_OR_EARLIER"


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [
        ("2026-07-19T00:00:00+08:00", "D-1_0000_1200"),
        ("2026-07-19T12:00:00+08:00", "D-1_1200_1500"),
        ("2026-07-19T15:00:00+08:00", "D-1_1500_1800"),
        ("2026-07-19T18:00:00+08:00", "D-1_1800_2400"),
        ("2026-07-20T08:00:00+08:00", "D0_0800_1000"),
        ("2026-07-20T10:00:00+08:00", "D0_1000_1100"),
        ("2026-07-20T12:00:00+08:00", "D0_1200_1300"),
        ("2026-07-20T14:00:00+08:00", "D0_1400_1500"),
        ("2026-07-20T16:00:00+08:00", "D0_1600_1800"),
    ],
)
def test_time_phase_boundaries(timestamp, expected):
    assert relative_phase(epoch(timestamp), "2026-07-20") == expected


def test_half_hour_distribution_boundary():
    assert half_hour_bin(epoch("2026-07-20T09:30:00+08:00")) == "09:30—10:00"


def test_price_up_add():
    assert classify_price_add(0.20, 0.21) == "PRICE_UP_ADD"


def test_price_down_add():
    assert classify_price_add(0.20, 0.19) == "PRICE_DOWN_ADD"


def test_price_flat_add():
    assert classify_price_add(0.20, 0.209) == "PRICE_FLAT_ADD"


def test_above_average_cost_add():
    assert classify_average_cost_add(0.20, 0.21) == "ABOVE_AVERAGE_COST_ADD"


def test_below_average_cost_add():
    assert classify_average_cost_add(0.20, 0.19) == "BELOW_AVERAGE_COST_ADD"


def test_near_average_cost_add():
    assert classify_average_cost_add(0.20, 0.209) == "NEAR_AVERAGE_COST_ADD"


def test_annotate_adds_preserves_first_buy():
    rows = [normalized_fill(), normalized_fill(timestamp="2026-07-20T09:01:00+08:00", price=0.21)]
    annotate_adds(rows)
    assert rows[0]["price_add_class"] == ""
    assert rows[1]["price_add_class"] == "PRICE_UP_ADD"


def test_adjacent_exact_buckets():
    assert buckets_adjacent(
        {"unit": "C", "bucket_kind": "exact", "bucket_low": 30},
        {"unit": "C", "bucket_kind": "exact", "bucket_low": 31},
    )


def test_tail_is_not_adjacent_exact_bucket():
    assert not buckets_adjacent(
        {"unit": "C", "bucket_kind": "below", "bucket_low": None},
        {"unit": "C", "bucket_kind": "exact", "bucket_low": 30},
    )


def test_parse_tail_bucket():
    parsed = parse_bucket(raw(
        slug="highest-temperature-in-beijing-on-july-20-2026-30corbelow",
        title="Will the highest temperature in Beijing be 30°C or below on July 20?",
    ))
    assert parsed["bucket_kind"] == "below"


def test_single_bucket_event_is_retained():
    metrics = bucket_metrics([normalized_fill()])
    assert metrics["basket_type"] == "SINGLE_BUCKET_ONLY"


def test_simultaneous_multi_bucket():
    metrics = bucket_metrics([
        normalized_fill(asset="a", bucket="30°C", bucket_low=30),
        normalized_fill(timestamp="2026-07-20T09:01:00+08:00", asset="b", bucket="31°C", bucket_low=31),
    ])
    assert metrics["basket_type"] == "SIMULTANEOUS_MULTI_BUCKET"


def test_single_then_adjacent_basket():
    metrics = bucket_metrics([
        normalized_fill(asset="a", bucket="30°C", bucket_low=30),
        normalized_fill(timestamp="2026-07-20T10:00:00+08:00", asset="b", bucket="31°C", bucket_low=31),
    ])
    assert metrics["basket_type"] == "SINGLE_THEN_ADJACENT_BASKET"


def test_bucket_rotation():
    metrics = bucket_metrics([
        normalized_fill(asset="a", bucket="30°C", bucket_low=30, usd=30),
        normalized_fill(timestamp="2026-07-20T10:00:00+08:00", asset="b", bucket="31°C", bucket_low=31, usd=70),
    ])
    assert metrics["bucket_rotation"]


def test_one_shot_entry():
    assert classify_entry([normalized_fill()], 2) == "ONE_SHOT_ENTRY"


def test_small_test_then_scale_entry():
    buys = [normalized_fill(usd=1), normalized_fill(timestamp="2026-07-20T10:00:00+08:00", usd=9)]
    assert classify_entry(buys, 10) == "SMALL_TEST_THEN_SCALE"


def test_quick_exit_within_one_hour():
    buys = [normalized_fill()]
    sells = [normalized_fill(timestamp="2026-07-20T09:30:00+08:00", side="SELL")]
    assert "QUICK_EXIT_WITHIN_1H" in classify_exit(buys, sells, 10)


def test_reentry_after_sell():
    buys = [
        normalized_fill(),
        normalized_fill(timestamp="2026-07-20T10:00:00+08:00"),
    ]
    sells = [normalized_fill(timestamp="2026-07-20T09:30:00+08:00", side="SELL")]
    assert "REENTRY_AFTER_SELL" in classify_exit(buys, sells, 20)


def test_no_sell_is_not_held_to_settlement():
    labels = classify_exit([normalized_fill()], [], 10)
    assert labels == ["NO_RECORDED_SELL", "FINAL_PATH_UNKNOWN"]


def test_unmatched_sell_marks_partial_timeline():
    rows = [
        normalized_fill(shares=5),
        normalized_fill(timestamp="2026-07-20T10:00:00+08:00", side="SELL", shares=10),
    ]
    summary = event_summary(rows[0]["event_key"], rows, {})
    assert summary["entry_timeline_status"] == "ENTRY_TIMELINE_PARTIAL_UNMATCHED_SELL"


def test_complete_timeline_keeps_no_sell_event():
    rows = [normalized_fill()]
    summary = event_summary(rows[0]["event_key"], rows, {})
    assert summary["entry_timeline_status"] == "ENTRY_TIMELINE_COMPLETE"
    assert summary["sell_fill_count"] == 0


def test_candidate_checkpoint_funds_before_and_after():
    rows = [
        normalized_fill(timestamp="2026-07-20T09:00:00+08:00", usd=4),
        normalized_fill(timestamp="2026-07-20T11:00:00+08:00", usd=6),
    ]
    event = event_summary(rows[0]["event_key"], rows, {})
    output = candidate_rows([event], {event["event_key"]: rows})
    selected = next(row for row in output if row["scope"] == "ALL_OBSERVABLE_EVENTS" and row["checkpoint"] == "D0_1000")
    assert selected["buy_usd_before"] == 4
    assert selected["buy_usd_after"] == 6


def test_candidate_has_complete_only_scope():
    rows = [normalized_fill()]
    event = event_summary(rows[0]["event_key"], rows, {})
    scopes = {row["scope"] for row in candidate_rows([event], {event["event_key"]: rows})}
    assert scopes == {"ALL_OBSERVABLE_EVENTS", "ENTRY_TIMELINE_COMPLETE_ONLY"}


def test_checkpoint_uses_beijing_timezone():
    assert checkpoint_epoch("2026-07-20", "D0_1000") == epoch("2026-07-20T10:00:00+08:00")


def test_archetype_marks_final_unknown_without_sell():
    rows = [normalized_fill()]
    event = event_summary(rows[0]["event_key"], rows, {})
    event["pnl_status"] = "OBSERVABLE_BUT_INCOMPLETE"
    assert "FINAL_PATH_UNKNOWN" in archetype_labels(event, rows)


def test_public_fill_vocabulary_does_not_use_order_count():
    module = Path("src/husky_beijing_full_trade_study_v1.py").read_text(encoding="utf-8")
    assert "public trade fill count; not original order count" in module
    assert "original_order_count" not in module


def test_safety_constants_are_false_for_trading_actions():
    from src import husky_beijing_full_trade_study_v1 as module
    assert module.PUBLIC_DATA_ONLY is True
    assert module.PUBLIC_GET_ONLY is True
    assert module.ACCOUNT_CONNECTION is False
    assert module.SIGNING is False
    assert module.REAL_ORDER is False
    assert module.FORMAL_STARTED is False
