# HUSKY STRATEGY EVIDENCE FAST v1

## Scope and safeguards

This is a descriptive study of recorded behavior. It does not use Husky history to create ZBAA weather probabilities and does not claim a strategy is profitable.

Weather-event identity is always `city + weather_date_local + weather_metric`; temperature buckets inside one event are not treated as independent experiments.

## Data directory and field mapping

| File | Bytes | Columns inspected | Read policy |
|---|---:|---|---|
| `data/raw/trades.csv` | 12589929 | asset, bio, conditionId, eventSlug, icon, name, outcome, outcomeIndex, price, profileImage, profileImageOptimized, proxyWallet, pseudonym, side, size, slug, timestamp, title, transactionHash | HEADER_ONLY_OR_STREAMED |
| `data/raw/activity.csv` | 13479240 | asset, bio, conditionId, eventSlug, icon, isCombo, name, outcome, outcomeIndex, price, profileImage, profileImageOptimized, proxyWallet, pseudonym, side, size, slug, timestamp, title, transactionHash, type, usdcSize | HEADER_ONLY_OR_STREAMED |
| `data/raw/closed_positions.csv` | 1308776 | asset, avgPrice, conditionId, curPrice, endDate, eventSlug, icon, oppositeAsset, oppositeOutcome, outcome, outcomeIndex, proxyWallet, realizedPnl, slug, timestamp, title, totalBought | HEADER_ONLY_OR_STREAMED |
| `data/raw/current_positions.csv` | 1014258 | asset, avgPrice, cashPnl, conditionId, curPrice, currentValue, endDate, eventId, eventSlug, icon, initialValue, mergeable, negativeRisk, oppositeAsset, oppositeOutcome, outcome, outcomeIndex, percentPnl, percentRealizedPnl, proxyWallet, realizedPnl, redeemable, size, slug, title, totalBought | HEADER_ONLY_OR_STREAMED |
| `data/processed/weather_trades_normalized.csv` | 16895905 | asset, bio, bucket_high, bucket_kind, bucket_label, bucket_low, city, conditionId, date_year_inferred, endDate, eventSlug, icon, market_end_epoch, name, notional_usd, observation_cutoff_epoch, observation_cutoff_utc, oppositeAsset, oppositeOutcome, outcome, outcomeIndex, price, profileImage, profileImageOptimized, proxyWallet, pseudonym, seen_in_closed_positions, seen_in_current_positions, seen_in_trades, side, size, slug, timestamp, timestamp_utc, title, transactionHash, unit, weather_date, weather_metric | HEADER_ONLY_OR_STREAMED |
| `data/processed/weather_position_lifecycle.csv` | 3295675 | asset, asset_won, authoritative_realized_pnl, bucket_high, bucket_kind, bucket_label, bucket_low, buy_after_market_end_count, buy_after_observation_proxy_count, buy_count, buy_shares, buy_usd, cashflow_pnl_before_transform_adjustment, cashflow_reliable, city, closed_avg_price, closed_cur_price, closed_implied_buy_usd, closed_position_realized_pnl, closed_total_bought_shares, conditionId, conversion_count, current_cash_pnl, current_initial_value, entry_price_bin, eventSlug, exit_mode, first_buy_ts, first_buy_utc, first_entry_lead_bin, first_entry_lead_hours, first_entry_lead_to_market_end_hours, first_trade_ts, first_trade_utc, last_trade_ts, last_trade_utc, market_end_epoch, market_end_utc, merge_count, net_traded_shares, non_transform_exit_mode, observation_cutoff_epoch, observation_cutoff_utc, outcome, pnl_status, redeem_activity_count_by_condition, sell_count, sell_shares, sell_usd, slug, split_count, title, transform_affected, unit, weather_date, weather_metric, weighted_avg_buy_price, weighted_avg_buy_ts, weighted_avg_buy_utc, weighted_avg_sell_price, weighted_entry_lead_bin, weighted_entry_lead_hours | HEADER_ONLY_OR_STREAMED |
| `data/processed/weather_city_day_baskets.csv` | 393284 | adjacent_exact_or_range, authoritative_realized_pnl_sum, bucket_count, buckets, buy_usd, cashflow_pnl_before_resolution, city, closed_yes_asset_count, has_transform_events, max_entry_state_payout, max_entry_state_pnl, max_remaining_state_payout, min_entry_state_payout, min_entry_state_pnl, min_remaining_state_payout, net_cost_after_pre_resolution_sells, open_or_unresolved_yes_asset_count, sell_usd, state_model_note, state_model_valid, unequal_share_ratio_entry, unit, weather_date, weather_metric, winner_bucket_labels, winner_in_visible_yes_basket | HEADER_ONLY_OR_STREAMED |
| `data/processed/city_day_pnl.csv` | 204287 | asset_count, buy_usd, city, closed_asset_count, closed_authoritative_pnl, exit_modes, has_open_or_unresolved, has_transform_events, losing_closed_assets, no_asset_count, open_mark_to_market_cash_pnl, open_or_unresolved_asset_count, sell_usd, transform_asset_count, unit, weather_date, weather_metric, winning_closed_assets, yes_asset_count | HEADER_ONLY_OR_STREAMED |
| `data/processed/profit_concentration.csv` | 1647 | gross_loss_pnl, gross_positive_pnl, items_with_pnl, leave_top1_out_pnl, level, top10_cutoff_id, top10_pnl, top10_share_of_gross_positive_pnl, top10_share_of_total_net_pnl, top1_id, top1_pnl, top1_share_of_gross_positive_pnl, top1_share_of_total_net_pnl, top5_cutoff_id, top5_pnl, top5_share_of_gross_positive_pnl, top5_share_of_total_net_pnl, total_net_pnl | HEADER_ONLY_OR_STREAMED |
| `data/exit_rule_grid_v4.csv` | 64968 | rule_id, rule_family, rule_description, split, price_scenario, haircut, group_type, group, positions, events, cities, price_bins, buy_usd, net_pnl, roi_on_buy_usd, win_rate, median_pnl, median_roi, max_single_loss, top1_profit_share_of_net, top5_profit_share_of_net, top10_profit_share_of_net, leave_top1_out_net_pnl, leave_top5_out_net_pnl, weather_date_sequence_drawdown, delta_vs_hold_pnl, saved_loser_to_profit_count, roundtrip_2x_loss_improved_count, roundtrip_2x_loss_saved_to_profit_count, premature_correct_sell_loss_count, premature_correct_sell_loss_usd, max_city_abs_pnl_share, max_price_bin_abs_pnl_share | HEADER_ONLY_OR_STREAMED |
| `data/exit_rule_position_detail_v4.csv` | 102497256 | asset, event_key, split, position_structure, city, weather_date, weather_metric, bucket_label, entry_price_bin, first_entry_lead_bin_local, local_weather_day_end_utc, rule_id, rule_family, price_scenario, haircut, history_status, history_points_pre_end, buy_count, buy_shares, buy_usd, weighted_avg_buy_price, settlement_price_v2, asset_won_v2, hold_to_settlement_pnl, actual_huskyvs_pnl_v2, simulated_pnl, simulated_roi, delta_vs_hold_pnl, simulated_sell_proceeds, simulated_settlement_value, simulated_sold_shares, simulated_remaining_shares, triggered_steps, first_trigger_utc, last_trigger_utc, first_trigger_price, last_trigger_price, max_price_pre_end, max_multiple_pre_end, saved_loser_to_profit, roundtrip_2x_loss_improved, roundtrip_2x_loss_saved_to_profit, premature_correct_sell_loss, no_future_sell_violation | STREAM_ONLY_NOT_USED |

Logical field mapping:
- `event_identity` ← city, weather_date, weather_metric
- `position_identity` ← asset, conditionId, eventSlug, slug
- `temperature_bucket` ← bucket_label, bucket_kind, bucket_low, bucket_high, outcome
- `entry_price` ← first BUY price from weather_trades_normalized.csv, weighted_avg_buy_price, avgPrice
- `exit_price` ← weighted_avg_sell_price, SELL price from weather_trades_normalized.csv
- `stake` ← buy_usd, initialValue, totalBought, usdcSize
- `sold_fraction` ← sell_shares / buy_shares
- `remaining_fraction` ← max(buy_shares - sell_shares, 0) / buy_shares
- `realized_pnl` ← authoritative_realized_pnl, realizedPnl
- `settlement_pnl` ← hold_to_settlement_pnl in exit-rule detail when available
- `trade_side` ← side
- `timestamps` ← timestamp, timestamp_utc, first_buy_utc, last_trade_utc

## Sample size

- Weather events: 1629
- Positions: 3663
- Beijing events: 50
- Station-confirmed ZBAA historical events: 0 (`NOT_SUPPORTED_BY_AVAILABLE_DATA`; old records do not prove station identity)
- Chronological split: first 1140 events (70%) for observation; final 489 events (30%) for independent checking.

## OBSERVED

- 1629 weather events and 3663 YES positions are directly recorded after normalization.
- 1037 events contain more than one purchased temperature bucket; 812 are adjacent by normalized interval.
- 2144 positions have more than one BUY record; 677 positions have a recorded partial sell.
- 2010 positions have no recorded SELL in the available trade window.
- Positions per event: median 2, range 1–10.
- Main bucket (largest recorded event stake) has at least one adjacent purchased bucket in 901 events.
- First BUY price: n=3654, median 0.0800, p90 0.4000.
- Per-bucket event stake fraction: median 0.3482, p10–p90 0.0421–1.0000.
- Recorded sold fraction: median 1.0000; weighted sell/buy multiple median 1.1139.
- Event PnL total: 37894.36; bootstrap 95% interval for mean event PnL: [29.408636, 39.301899].
- Largest winner share of gross positive PnL: 0.0220.
- PnL after removing top 1 / top 5 positive events: 36951.79 / 34231.92.
- Maximum consecutive losing events: {'events': 6, 'cumulative_pnl': -115.421564, 'ending_event': ['Panama City', '2026-04-21', 'high']}.

## INFERRED — at most three forward hypotheses

### H1_ADJACENT_BASKET

Husky often combines adjacent temperature buckets within one weather event.

Train rate: 0.4561; held-out rate: 0.5971. Result: `VALIDATED_AS_RECURRING_BEHAVIOR`. Profitability: `NOT_SUPPORTED_BY_AVAILABLE_DATA`.

### H2_PARTIAL_EXIT_AT_2X

Partial exits and exits around/above 2x are candidates for forward testing, not proven rules.

Train rate: 0.1632; held-out rate: 0.1104. Result: `NOT_VALIDATED_OUT_OF_SAMPLE`. Profitability: `NOT_SUPPORTED_BY_AVAILABLE_DATA`.

### H3_HOLD_TO_SETTLEMENT

Holding all recorded shares without a pre-resolution sell is a recurring behavior.

Train rate: 0.3061; held-out rate: 0.6115. Result: `BASELINE_BEHAVIOR_ONLY_NOT_PROFITABILITY_VALIDATION`. Profitability: `NOT_SUPPORTED_BY_AVAILABLE_DATA`.

## Fixed exit-rule held-out comparison

The existing v4 grid is read-only evidence. Its chronological train/validation split and fixed 2x rules are reused without tuning. Positive sampled PnL alone is not treated as validation when the 0.8 executable-price haircut fails versus HOLD.

| Rule | Validation sampled net PnL | Delta vs HOLD | Validation 0.8-haircut net PnL | 0.8 delta vs HOLD | Result |
|---|---:|---:|---:|---:|---|
| HOLD | 6609.98 | 0.00 | 6609.98 | 0.00 | `HOLD_COMPARATOR_BASELINE` |
| DOUBLE_SELL_50 | 7201.58 | 591.60 | 4170.94 | -2439.04 | `NOT_VALIDATED_OUT_OF_SAMPLE` |
| DOUBLE_SELL_75 | 7497.38 | 887.40 | 2951.42 | -3658.56 | `NOT_VALIDATED_OUT_OF_SAMPLE` |

## NOT_SUPPORTED

- Husky's private intent, psychology, unpublished bankroll, or fixed decision rule.: `NOT_SUPPORTED_BY_AVAILABLE_DATA`
- A historical weather-probability edge at entry because no contemporaneous Husky weather probability series is available.: `NOT_SUPPORTED_BY_AVAILABLE_DATA`
- ZBAA station identity for Beijing history when the historical records identify only the city/market.: `NOT_SUPPORTED_BY_AVAILABLE_DATA`
- A causal or profitable strategy rule from descriptive historical behavior.: `NOT_SUPPORTED_BY_AVAILABLE_DATA`
- Whether every position with no recorded SELL was actually held through final settlement.: `NOT_SUPPORTED_BY_AVAILABLE_DATA`
- Husky's intended stake when only actual fills/cash flows were recorded.: `NOT_SUPPORTED_BY_AVAILABLE_DATA`

## Slippage sensitivity

No entry-time historical order books are available. The figures below are mechanical sensitivity assumptions, not observed fills:

`{'plus_0c': 37894.360569, 'plus_1c': 29330.711739, 'plus_2c': 20767.06291}`

## Overfitting risk

The same account produced all observations, outcomes are clustered within weather events, station identity is often absent, and exploratory history cannot prove future profitability. Parameters for the shadow phase are therefore fixed before results: EDGE_05/10/15, MAIN_ONLY, TOP2_ADJACENT 70/30, HOLD, DOUBLE_SELL_50, and DOUBLE_SELL_75.

`GENERAL_HUSKY_HYPOTHESIS_ONLY`: ZBAA-specific history is insufficient; only future ZBAA shadow samples can validate these candidates.
