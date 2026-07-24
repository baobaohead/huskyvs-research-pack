import math

from src.backtest_exit_rules_v4 import Rule, simulate_rule, split_events


def base_position():
    return {
        "asset": "asset-1",
        "asset_won_v2": "False",
        "buy_shares": "200",
        "buy_usd": "20",
        "weighted_avg_buy_price": "0.1",
        "settlement_price_v2": "0",
        "realized_pnl_v2": "-20",
        "sell_shares": "0",
        "first_sell_ts": "",
        "last_sell_ts": "",
        "local_weather_day_end_epoch": "1000",
        "weather_date": "2026-01-01",
        "city": "A",
        "weather_metric": "high",
        "unit": "C",
    }


def test_v4_split_keeps_same_event_together():
    rows = []
    for day in range(10):
        for bucket in range(3):
            rows.append(
                {
                    "weather_date": f"2026-01-{day + 1:02d}",
                    "city": "City",
                    "weather_metric": "high",
                    "unit": "C",
                    "bucket_label": str(bucket),
                }
            )

    train, validation = split_events(rows, train_ratio=0.7)

    assert len(train) == 7
    assert len(validation) == 3
    assert train.isdisjoint(validation)


def test_v4_simulation_does_not_sell_future_buys():
    row = base_position()
    buy_fills = [
        {"timestamp": 100, "price": 0.1, "shares": 100.0},
        {"timestamp": 300, "price": 0.1, "shares": 100.0},
    ]
    price_points = [
        {"timestamp": 200, "price": 0.2},
    ]
    rule = Rule(
        rule_id="test_2x_sell100",
        family="test",
        description="test",
        steps=((2.0, 1.0),),
    )

    result = simulate_rule(row, buy_fills, price_points, rule, "sampled_1_0", 1.0)

    assert result.simulated_sold_shares == 100
    assert result.simulated_remaining_shares == 100
    assert not result.no_future_sell_violation


def test_v4_haircut_can_prevent_threshold_trigger():
    row = base_position()
    buy_fills = [{"timestamp": 100, "price": 0.1, "shares": 100.0}]
    price_points = [{"timestamp": 200, "price": 0.2}]
    rule = Rule(
        rule_id="test_2x_sell100",
        family="test",
        description="test",
        steps=((2.0, 1.0),),
    )

    full_price = simulate_rule(row, buy_fills, price_points, rule, "sampled_1_0", 1.0)
    haircut = simulate_rule(row, buy_fills, price_points, rule, "haircut_0_8", 0.8)

    assert full_price.simulated_sold_shares == 100
    assert haircut.simulated_sold_shares == 0
    assert math.isnan(haircut.first_trigger_price)


def test_v4_recover_principal_sells_only_needed_shares():
    row = base_position()
    row.update({"buy_shares": "100", "buy_usd": "10"})
    buy_fills = [{"timestamp": 100, "price": 0.1, "shares": 100.0}]
    price_points = [{"timestamp": 200, "price": 0.2}]
    rule = Rule(
        rule_id="recover_principal_keep_free",
        family="test",
        description="test",
        recover_principal=True,
    )

    result = simulate_rule(row, buy_fills, price_points, rule, "sampled_1_0", 1.0)

    assert result.simulated_sold_shares == 50
    assert result.simulated_remaining_shares == 50
    assert result.simulated_pnl == 0
