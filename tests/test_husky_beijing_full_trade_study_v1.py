from __future__ import annotations

import csv
import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.husky_beijing_full_trade_study_v1 import (
    HUSKY_WALLET,
    PORTABLE_EVIDENCE_SCHEMA,
    Window,
    activity_join_key,
    analyze,
    annotate_adds,
    archetype_labels,
    attach_pnl,
    bucket_metrics,
    buckets_adjacent,
    candidate_rows,
    checkpoint_epoch,
    classify_average_cost_add,
    classify_entry,
    classify_event_position,
    classify_exit,
    classify_position_row,
    classify_price_add,
    deduplicate_records,
    event_key,
    event_summary,
    fetch_activity_window,
    final_path_classification,
    half_hour_bin,
    is_beijing_highest_market,
    merge_public_fills,
    load_saved_public_evidence,
    parse_bucket,
    parse_weather_date,
    position_snapshot_pnl,
    reconcile_resolved_pnl_formulas,
    recorded_sell_realized_pnl,
    relative_phase,
    stable_trade_key,
    threshold_timestamp,
    timing_comparison,
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
    assert labels == ["NO_RECORDED_SELL"]


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


def test_candidate_continues_buying_excludes_first_entry_after_checkpoint():
    before_and_after = [
        normalized_fill(timestamp="2026-07-20T09:00:00+08:00", usd=4),
        normalized_fill(timestamp="2026-07-20T11:00:00+08:00", usd=6),
    ]
    first_after = [
        normalized_fill(
            timestamp="2026-07-20T11:30:00+08:00",
            asset="asset-late",
            usd=5,
        )
    ]
    first_event = event_summary(
        before_and_after[0]["event_key"], before_and_after, {}
    )
    second_event = event_summary("late-event", first_after, {})
    second_event["event_key"] = "late-event"
    output = candidate_rows(
        [first_event, second_event],
        {
            first_event["event_key"]: before_and_after,
            second_event["event_key"]: first_after,
        },
    )
    selected = next(
        row for row in output
        if row["scope"] == "ALL_OBSERVABLE_EVENTS"
        and row["checkpoint"] == "D0_1000"
    )
    assert selected["continues_buying_after_event_count"] == 1
    assert selected["first_entry_after_event_count"] == 1


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
    assert "NO_RECORDED_SELL_FINAL_PATH_UNKNOWN" in archetype_labels(event, rows)


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


def position_row(
    *,
    asset: str = "asset-30",
    end_date: str = "2026-07-20",
    redeemable: bool | None = True,
    cash_pnl: float = -2,
    realized_pnl: float = 0.5,
    current_value: float = 0,
    initial_value: float = 2,
) -> dict:
    return {
        "asset": asset,
        "endDate": end_date,
        "redeemable": redeemable,
        "cashPnl": cash_pnl,
        "realizedPnl": realized_pnl,
        "currentValue": current_value,
        "initialValue": initial_value,
    }


def test_redeemable_past_enddate_is_resolved_unredeemed():
    status = classify_position_row(
        position_row(),
        "2026-07-29T00:00:00+00:00",
    )
    assert status == "RESOLVED_REDEEMABLE_UNREDEEMED"


def test_future_enddate_is_active_open():
    status = classify_position_row(
        position_row(end_date="2026-07-30", redeemable=False),
        "2026-07-29T00:00:00+00:00",
    )
    assert status == "ACTIVE_OPEN_CONFIRMED"


def test_past_enddate_without_resolved_status_is_unknown():
    status = classify_position_row(
        position_row(redeemable=False),
        "2026-07-29T00:00:00+00:00",
    )
    assert status == "PAST_ENDDATE_STATUS_UNKNOWN"


def test_resolved_unredeemed_never_counts_as_active_open():
    status, assets = classify_event_position(
        {"asset-30"},
        {"asset-30": position_row()},
        "2026-07-29T00:00:00+00:00",
        strict_closed=False,
    )
    assert status == "RESOLVED_REDEEMABLE_UNREDEEMED"
    assert "ACTIVE_OPEN_CONFIRMED" not in set(assets.values())


def test_active_and_resolved_snapshot_pnl_are_separate(monkeypatch):
    from src import husky_beijing_full_trade_study_v1 as module

    monkeypatch.setattr(module, "read_csv", lambda _: [])
    rows = [normalized_fill()]
    event = event_summary(rows[0]["event_key"], rows, {})
    active = attach_pnl(
        [event], {event["event_key"]: rows}, Path("."),
        {"asset-30": position_row(end_date="2026-07-30")},
        {}, "2026-07-29T00:00:00+00:00",
    )[0]
    resolved = attach_pnl(
        [event], {event["event_key"]: rows}, Path("."),
        {"asset-30": position_row()},
        {}, "2026-07-29T00:00:00+00:00",
    )[0]
    assert active["active_open_mark_to_market_pnl"] == -1.5
    assert active["resolved_redeemable_snapshot_pnl"] is None
    assert resolved["active_open_mark_to_market_pnl"] is None
    assert resolved["resolved_redeemable_snapshot_pnl"] == -1.5


def test_four_resolved_pnl_formulas_reconcile_against_authority():
    row = position_row(
        cash_pnl=5,
        realized_pnl=2,
        current_value=8,
        initial_value=5,
    )
    result = reconcile_resolved_pnl_formulas(
        [row],
        {"asset-30": {"realizedPnl": 7}},
    )
    assert position_snapshot_pnl(row, "A_cashPnl") == 5
    assert position_snapshot_pnl(row, "B_realizedPnl") == 2
    assert position_snapshot_pnl(row, "C_cashPnl_plus_realizedPnl") == 7
    assert position_snapshot_pnl(
        row, "D_currentValue_minus_initialValue_plus_realizedPnl"
    ) == 5
    assert result["most_stable_formula"] == "C_cashPnl_plus_realizedPnl"
    assert result["validation_result"] == "RESOLVED_UNREDEEMED_PNL_VALIDATED"


def test_unvalidated_resolved_pnl_is_not_strict(monkeypatch):
    from src import husky_beijing_full_trade_study_v1 as module

    monkeypatch.setattr(module, "read_csv", lambda _: [])
    rows = [normalized_fill()]
    event = event_summary(rows[0]["event_key"], rows, {})
    attached = attach_pnl(
        [event], {event["event_key"]: rows}, Path("."),
        {"asset-30": position_row()}, {},
        "2026-07-29T00:00:00+00:00",
    )[0]
    assert attached["resolved_redeemable_snapshot_pnl"] == -1.5
    assert attached["strict_pnl"] is None
    assert attached["pnl_status"] == "RESOLVED_REDEEMABLE_UNREDEEMED"


def test_partial_exit_is_explicitly_labeled():
    rows = [
        normalized_fill(shares=10),
        normalized_fill(
            timestamp="2026-07-20T10:00:00+08:00",
            side="SELL",
            shares=5,
        ),
    ]
    event = event_summary(rows[0]["event_key"], rows, {})
    assert "PARTIAL_EXIT_OBSERVED" in event["exit_classifications"]


def test_profitable_partial_sell_uses_recorded_sell_pnl():
    rows = [
        normalized_fill(price=0.2, shares=10, usd=2),
        normalized_fill(
            timestamp="2026-07-20T10:00:00+08:00",
            side="SELL", price=0.4, shares=5, usd=2,
        ),
    ]
    event = event_summary(rows[0]["event_key"], rows, {})
    labels = archetype_labels(event, rows)
    assert event["recorded_sell_realized_pnl_fifo"] == pytest.approx(1)
    assert event["recorded_sell_realized_pnl_average_cost"] == pytest.approx(1)
    assert "PROFITABLE_PARTIAL_SELL_OBSERVED" in labels


def test_loss_realizing_partial_sell_uses_recorded_sell_pnl():
    rows = [
        normalized_fill(price=0.4, shares=10, usd=4),
        normalized_fill(
            timestamp="2026-07-20T10:00:00+08:00",
            side="SELL", price=0.2, shares=5, usd=1,
        ),
    ]
    event = event_summary(rows[0]["event_key"], rows, {})
    labels = archetype_labels(event, rows)
    assert event["recorded_sell_realized_pnl_fifo"] == pytest.approx(-1)
    assert event["recorded_sell_realized_pnl_average_cost"] == pytest.approx(-1)
    assert "LOSS_REALIZING_PARTIAL_SELL_OBSERVED" in labels


def test_final_event_profit_does_not_imply_profit_exit():
    rows = [normalized_fill()]
    event = event_summary(rows[0]["event_key"], rows, {})
    event["strict_pnl"] = 100
    labels = archetype_labels(event, rows)
    assert "PROFITABLE_PARTIAL_SELL_OBSERVED" not in labels


def test_final_event_loss_does_not_imply_loss_cut():
    rows = [normalized_fill()]
    event = event_summary(rows[0]["event_key"], rows, {})
    event["strict_pnl"] = -100
    labels = archetype_labels(event, rows)
    assert "LOSS_REALIZING_PARTIAL_SELL_OBSERVED" not in labels


def test_hold_to_settlement_and_unknown_path_are_mutually_exclusive():
    rows = [normalized_fill()]
    event = event_summary(rows[0]["event_key"], rows, {})
    event["held_to_settlement_observed"] = True
    event["final_path_classification"] = final_path_classification(event)
    labels = archetype_labels(event, rows)
    assert "HOLD_TO_SETTLEMENT_OBSERVED" in labels
    assert "NO_RECORDED_SELL_FINAL_PATH_UNKNOWN" not in labels


def test_no_sell_with_authoritative_resolution_is_held():
    event = {"sell_fill_count": 0, "held_to_settlement_observed": True}
    assert final_path_classification(event) == "HOLD_TO_SETTLEMENT_OBSERVED"


def test_no_sell_without_resolution_is_final_path_unknown():
    event = {"sell_fill_count": 0, "held_to_settlement_observed": False}
    assert (
        final_path_classification(event)
        == "NO_RECORDED_SELL_FINAL_PATH_UNKNOWN"
    )


def test_strict_pnl_complete_only_is_primary_timing_scope():
    complete_rows = [normalized_fill()]
    partial_rows = [
        normalized_fill(asset="asset-partial", shares=5),
        normalized_fill(
            timestamp="2026-07-20T10:00:00+08:00",
            asset="asset-partial", side="SELL", shares=10,
        ),
    ]
    complete = event_summary(complete_rows[0]["event_key"], complete_rows, {})
    partial = event_summary(partial_rows[0]["event_key"], partial_rows, {})
    complete.update({"strict_pnl": 1, "price_up_add_count": 0, "price_down_add_count": 0, "price_flat_add_count": 0})
    partial.update({"strict_pnl": 2, "price_up_add_count": 0, "price_down_add_count": 0, "price_flat_add_count": 0})
    result = timing_comparison([complete, partial])
    assert result["STRICT_PNL_ENTRY_COMPLETE_ONLY"]["profit_events"]["event_count"] == 1
    assert result["STRICT_PNL_ALL"]["profit_events"]["event_count"] == 2


def test_partial_entry_timeline_is_excluded_from_primary_timing():
    rows = [
        normalized_fill(shares=5),
        normalized_fill(
            timestamp="2026-07-20T10:00:00+08:00",
            side="SELL", shares=10,
        ),
    ]
    event = event_summary(rows[0]["event_key"], rows, {})
    event.update({"strict_pnl": -1, "price_up_add_count": 0, "price_down_add_count": 0, "price_flat_add_count": 0})
    result = timing_comparison([event])
    assert result["STRICT_PNL_ENTRY_COMPLETE_ONLY"]["loss_events"]["event_count"] == 0
    assert result["STRICT_PNL_ALL"]["loss_events"]["event_count"] == 1


def test_candidate_report_uses_complete_only_scope():
    from src.husky_beijing_full_trade_study_v1 import render_report
    import inspect

    source = inspect.getsource(render_report)
    assert 'row["scope"] == "ENTRY_TIMELINE_COMPLETE_ONLY"' in source


def test_core_beijing_counts_remain_50_events_and_537_fills():
    report = json.loads(
        Path("docs/HUSKY_BEIJING_FULL_TRADE_STUDY_v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["beijing_event_count"] == 50
    assert report["total_public_fill_count"] == 537


def test_recorded_sell_pnl_reports_method_disagreement():
    rows = [
        normalized_fill(price=0.1, shares=10, usd=1),
        normalized_fill(
            timestamp="2026-07-20T09:10:00+08:00",
            price=0.9, shares=10, usd=9,
        ),
        normalized_fill(
            timestamp="2026-07-20T10:00:00+08:00",
            side="SELL", price=0.6, shares=10, usd=6,
        ),
    ]
    result = recorded_sell_realized_pnl(rows)
    assert result["recorded_sell_realized_pnl_fifo"] == pytest.approx(5)
    assert result["recorded_sell_realized_pnl_average_cost"] == pytest.approx(1)
    assert result["sell_pnl_method_disagreement"] is True


PORTABLE_EVIDENCE_DIR = Path(
    "docs/husky_beijing_full_trade_study_v1/saved_evidence_v1"
)
PORTABLE_TEST_TMP = Path("/tmp/husky_beijing_portable_evidence_fix/tests")


@pytest.fixture
def portable_package_copy():
    PORTABLE_TEST_TMP.mkdir(parents=True, exist_ok=True)
    parent = Path(tempfile.mkdtemp(prefix="package.", dir=PORTABLE_TEST_TMP))
    target = parent / "saved_evidence_v1"
    shutil.copytree(PORTABLE_EVIDENCE_DIR, target)
    yield target
    shutil.rmtree(parent)


def rewrite_manifest(directory: Path, mutate) -> Path:
    path = directory / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    mutate(manifest)
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def test_portable_relative_paths_resolve_from_manifest_directory():
    manifest, evidence = load_saved_public_evidence(
        PORTABLE_EVIDENCE_DIR / "manifest.json"
    )
    assert manifest["schema_version"] == PORTABLE_EVIDENCE_SCHEMA
    assert len(evidence["trades"]) == 537


def test_portable_manifest_can_move_with_its_directory(portable_package_copy):
    _, evidence = load_saved_public_evidence(
        portable_package_copy / "manifest.json"
    )
    assert len(evidence["activity"]) == 537


def test_absolute_evidence_path_is_rejected(portable_package_copy):
    path = rewrite_manifest(
        portable_package_copy,
        lambda manifest: manifest["aggregates"]["trades"].update(
            {"relative_path": "/tmp/not-portable.json"}
        ),
    )
    with pytest.raises(RuntimeError, match="NON_PORTABLE_ABSOLUTE_EVIDENCE_PATH"):
        load_saved_public_evidence(path)


def test_legacy_absolute_evidence_manifest_is_rejected(portable_package_copy):
    def mutate(manifest):
        manifest["schema_version"] = "husky_beijing_public_evidence_v1"
        manifest["aggregates"]["trades"]["path"] = "/tmp/legacy-trades.json"
        manifest["aggregates"]["trades"].pop("relative_path")

    path = rewrite_manifest(portable_package_copy, mutate)
    with pytest.raises(RuntimeError, match="NON_PORTABLE_ABSOLUTE_EVIDENCE_PATH"):
        load_saved_public_evidence(path)


def test_parent_path_traversal_is_rejected(portable_package_copy):
    path = rewrite_manifest(
        portable_package_copy,
        lambda manifest: manifest["aggregates"]["trades"].update(
            {"relative_path": "../outside.json"}
        ),
    )
    with pytest.raises(RuntimeError, match="EVIDENCE_PATH_TRAVERSAL_REJECTED"):
        load_saved_public_evidence(path)


def test_missing_portable_evidence_file_is_rejected(portable_package_copy):
    (portable_package_copy / "beijing_trades.json").unlink()
    with pytest.raises(RuntimeError, match="EVIDENCE_FILE_MISSING:trades"):
        load_saved_public_evidence(portable_package_copy / "manifest.json")


def test_portable_evidence_sha_mismatch_is_rejected(portable_package_copy):
    path = portable_package_copy / "beijing_trades.json"
    path.write_text("[]\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="EVIDENCE_SHA256_MISMATCH:trades"):
        load_saved_public_evidence(portable_package_copy / "manifest.json")


def test_portable_evidence_record_count_mismatch_is_rejected(
    portable_package_copy,
):
    path = rewrite_manifest(
        portable_package_copy,
        lambda manifest: manifest["aggregates"]["trades"].update(
            {"record_count": 536}
        ),
    )
    with pytest.raises(RuntimeError, match="EVIDENCE_RECORD_COUNT_MISMATCH:trades"):
        load_saved_public_evidence(path)


def test_portable_manifest_wallet_conflict_is_rejected(portable_package_copy):
    path = rewrite_manifest(
        portable_package_copy,
        lambda manifest: manifest.update({"wallet": "0x" + "1" * 40}),
    )
    with pytest.raises(RuntimeError, match="EVIDENCE_WALLET_MISMATCH:manifest"):
        load_saved_public_evidence(path)


def test_portable_manifest_cutoff_conflict_is_rejected():
    with pytest.raises(RuntimeError, match="EVIDENCE_ANALYSIS_CUTOFF_MISMATCH"):
        load_saved_public_evidence(
            PORTABLE_EVIDENCE_DIR / "manifest.json",
            expected_analysis_cutoff_utc="2026-07-30T00:00:00+00:00",
        )


def test_portable_trade_and_activity_are_only_beijing_highest():
    _, evidence = load_saved_public_evidence(
        PORTABLE_EVIDENCE_DIR / "manifest.json"
    )
    assert all(is_beijing_highest_market(row) for row in evidence["trades"])
    assert all(
        row.get("type") == "TRADE" and is_beijing_highest_market(row)
        for row in evidence["activity"]
    )


def test_portable_evidence_contains_only_husky_wallet():
    _, evidence = load_saved_public_evidence(
        PORTABLE_EVIDENCE_DIR / "manifest.json"
    )
    for payload in evidence.values():
        rows = payload if isinstance(payload, list) else [payload]
        assert {
            str(row.get("proxyWallet")).lower()
            for row in rows
            if row.get("proxyWallet")
        } <= {HUSKY_WALLET}


def test_portable_positions_only_use_observed_beijing_assets():
    _, evidence = load_saved_public_evidence(
        PORTABLE_EVIDENCE_DIR / "manifest.json"
    )
    observed = {
        str(row["asset"])
        for row in [*evidence["trades"], *evidence["activity"]]
    }
    assert {str(row["asset"]) for row in evidence["positions"]} <= observed


def test_portable_closed_positions_only_use_observed_beijing_assets():
    _, evidence = load_saved_public_evidence(
        PORTABLE_EVIDENCE_DIR / "manifest.json"
    )
    observed = {
        str(row["asset"])
        for row in [*evidence["trades"], *evidence["activity"]]
    }
    assert {str(row["asset"]) for row in evidence["closed_positions"]} <= observed


def run_portable_analysis(output_dir: Path):
    manifest = json.loads(
        (PORTABLE_EVIDENCE_DIR / "manifest.json").read_text(encoding="utf-8")
    )
    return analyze(
        Path(".").resolve(),
        output_dir / "output",
        output_dir / "summary.md",
        output_dir / "summary.json",
        analysis_started_at_utc=manifest["analysis_started_at_utc"],
        analysis_cutoff_utc=manifest["analysis_cutoff_utc"],
        evidence_manifest=(PORTABLE_EVIDENCE_DIR / "manifest.json").resolve(),
    )


def test_portable_offline_analysis_makes_zero_network_calls(monkeypatch):
    from src import husky_beijing_full_trade_study_v1 as module

    calls = []

    def fail_network(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("offline analysis attempted a network call")

    monkeypatch.setattr(module.urllib.request, "urlopen", fail_network)
    PORTABLE_TEST_TMP.mkdir(parents=True, exist_ok=True)
    parent = Path(tempfile.mkdtemp(prefix="offline.", dir=PORTABLE_TEST_TMP))
    try:
        summary = run_portable_analysis(parent)
    finally:
        shutil.rmtree(parent)
    assert calls == []
    assert summary["beijing_event_count"] == 50


def test_portable_core_statistics_match_reviewed_full_evidence():
    reviewed = json.loads(
        Path("docs/HUSKY_BEIJING_FULL_TRADE_STUDY_v1.json").read_text(
            encoding="utf-8"
        )
    )
    PORTABLE_TEST_TMP.mkdir(parents=True, exist_ok=True)
    parent = Path(tempfile.mkdtemp(prefix="equivalence.", dir=PORTABLE_TEST_TMP))
    try:
        replay = run_portable_analysis(parent)
    finally:
        shutil.rmtree(parent)
    keys = (
        "beijing_event_count",
        "total_public_fill_count",
        "public_buy_fill_count",
        "public_sell_fill_count",
        "entry_timeline_complete_event_count",
        "resolved_redeemable_event_count",
        "strict_closed_settled_event_count",
        "beijing_total_pnl_strict",
        "active_open_event_count",
        "beijing_first_observed_public_trade_utc",
        "beijing_last_observed_public_trade_utc",
        "d_minus_1_buy_usd_share",
        "d0_buy_usd_share",
    )
    assert {key: replay[key] for key in keys} == {
        key: reviewed[key] for key in keys
    }


def test_portable_replay_keeps_50_events_and_537_fills():
    reviewed = json.loads(
        Path("docs/HUSKY_BEIJING_FULL_TRADE_STUDY_v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert reviewed["beijing_event_count"] == 50
    assert reviewed["total_public_fill_count"] == 537


def test_portable_replay_keeps_14_strict_pnl_events():
    reviewed = json.loads(
        Path("docs/HUSKY_BEIJING_FULL_TRADE_STUDY_v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert reviewed["strict_closed_settled_event_count"] == 14
    assert reviewed["beijing_total_pnl_strict"] == pytest.approx(99.198968)


def test_portable_replay_keeps_36_resolved_redeemable_events():
    reviewed = json.loads(
        Path("docs/HUSKY_BEIJING_FULL_TRADE_STUDY_v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert reviewed["resolved_redeemable_event_count"] == 36
    assert reviewed["active_open_event_count"] == 0


def test_portable_candidate_checkpoints_match_reviewed_output():
    reviewed = Path(
        "docs/husky_beijing_full_trade_study_v1/"
        "beijing_candidate_checkpoints.csv"
    )
    PORTABLE_TEST_TMP.mkdir(parents=True, exist_ok=True)
    parent = Path(tempfile.mkdtemp(prefix="candidates.", dir=PORTABLE_TEST_TMP))
    try:
        run_portable_analysis(parent)
        portable = parent / "output" / "beijing_candidate_checkpoints.csv"
        assert portable.read_bytes() == reviewed.read_bytes()
    finally:
        shutil.rmtree(parent)


def test_portable_event_statuses_and_exit_labels_match_reviewed_output():
    reviewed = json.loads(
        Path("docs/HUSKY_BEIJING_FULL_TRADE_STUDY_v1.json").read_text(
            encoding="utf-8"
        )
    )
    PORTABLE_TEST_TMP.mkdir(parents=True, exist_ok=True)
    parent = Path(tempfile.mkdtemp(prefix="event-labels.", dir=PORTABLE_TEST_TMP))
    try:
        portable = run_portable_analysis(parent)
    finally:
        shutil.rmtree(parent)
    fields = (
        "position_status",
        "final_path_classification",
        "sell_pnl_status",
        "strategy_archetypes",
        "strict_pnl",
    )
    portable_events = {row["event_key"]: row for row in portable["events"]}
    reviewed_events = {row["event_key"]: row for row in reviewed["events"]}
    assert portable_events.keys() == reviewed_events.keys()
    assert all(
        tuple(portable_events[key].get(field) for field in fields)
        == tuple(reviewed_events[key].get(field) for field in fields)
        for key in reviewed_events
    )
