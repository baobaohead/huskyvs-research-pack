# HUSKYVS_FULL_AUDIT_v1

Generated at: 2026-07-20T08:06:37.474801+00:00
Wallet: `0xaf17116ae2b1476032785a67bd5b7c8c05905c20`
Period: `1761955200` to `1784592000`

This report is a public-data audit only. It does not include copy-trading, staking, or funding advice.

## 1. Data Coverage and Integrity

- Raw rows: trades 18308, activity 19498, current positions 1653, closed positions 2197.
- Weather rows parsed: trades 17964, assets 3663, city-day baskets 1565.
- `takerOnly=false`: True.
- Activity includes TRADE/SPLIT/MERGE/REDEEM: True.
- CSV/JSONL/manifest row counts consistent: True.
- Offset truncation audit passed: True.
- Accounting snapshot ZIP valid: True.

Duplicate audit: trades have no duplicate groups under transactionHash/asset/side/timestamp/size/price. Activity duplicate groups are REDEEM rows with blank asset/side/price, where a single redemption transaction references multiple titles; they were not treated as duplicated fills.

## 2. Low-Price YES by Exit Mode

Low-price YES was not uniformly positive. In this realized public sample, some low-price bins are positive after exits, but losses remain visible and transform-affected rows must be interpreted separately.

| Price | Exit | Closed | PnL | ROI | Win |
| --- | --- | --- | --- | --- | --- |
| 0-1c | hold_to_resolution | 78 | $426.54 | 279.7% | 2.6% |
| 0-1c | mixed_sell_and_resolution | 58 | $1,678.24 | 894.6% | 44.8% |
| 0-1c | transform_affected | 3 | $515.60 | 11292.5% | 100.0% |
| 1-2c | hold_to_resolution | 63 | $-111.30 | -57.5% | 3.2% |
| 1-2c | mixed_sell_and_resolution | 80 | $488.06 | 95.6% | 46.2% |
| 1-2c | transform_affected | 36 | $2,614.87 | 4749.5% | 100.0% |
| 2-5c | hold_to_resolution | 83 | $405.01 | 110.7% | 10.8% |
| 2-5c | mixed_sell_and_resolution | 162 | $5,473.85 | 381.5% | 51.9% |
| 2-5c | transform_affected | 14 | $1,040.18 | 1287.0% | 100.0% |
| 5-10c | hold_to_resolution | 66 | $806.10 | 134.8% | 15.2% |
| 5-10c | mixed_sell_and_resolution | 222 | $5,406.67 | 183.1% | 53.2% |
| 5-10c | transform_affected | 2 | $218.70 | 610.9% | 100.0% |
| 10-20c | hold_to_resolution | 93 | $2,785.76 | 182.8% | 35.5% |
| 10-20c | mixed_sell_and_resolution | 311 | $7,962.24 | 137.0% | 57.6% |
| 10-20c | transform_affected | 5 | $219.86 | 161.3% | 100.0% |
| >=20c | hold_to_resolution | 57 | $1,668.55 | 61.8% | 64.9% |
| >=20c | mixed_sell_and_resolution | 308 | $5,645.25 | 48.4% | 64.6% |
| >=20c | transform_affected | 11 | $566.44 | 52.5% | 90.9% |

Positive low-price YES groups: 11; negative low-price YES groups: 1. This supports a signal-filtered tail strategy more than indiscriminate cheap-YES buying.

## 3. Multi-City Volatility

Closed daily portfolio PnL totals $37,894.36 across 170 weather dates, with daily standard deviation $256.42. Max drawdown on closed city-day PnL is $371.01, from 2026-04-16 to 2026-04-17.
Pairwise city correlations with at least five overlapping city-days average 0.033 across 210 pairs.
The correlation layer generally supports diversification. The concentration layer below shows this is not a one-winner result, although the top winners still contribute a visible share of net profit.

## 4. Basket Counterfactuals

Evaluable non-overlapping resolved YES baskets: 291.

| Model | Observed-state PnL |
| --- | --- |
| actual_authoritative_realized | $8,639.90 |
| actual_entry_allocation_state_model | $6,463.52 |
| single_main_bucket | $6,587.43 |
| equal_dollar_basket | $15,653.58 |
| equal_payout_basket | $7,233.13 |

The unequal adjacent-basket structure should be analyzed as state-dependent payoffs, not as a simple sum of prices. Overlapping buckets such as `or below` and `or higher` are flagged and excluded from the clean counterfactual aggregate.

## 5. Profit Concentration

| Level | Total PnL | Top1 / Net | Top5 / Net | Top10 / Net | Leave Top1 Out |
| --- | --- | --- | --- | --- | --- |
| position | $37,894.36 | 2.6% | 9.7% | 16.3% | $36,922.56 |
| basket | $37,894.36 | 2.5% | 9.7% | 16.6% | $36,951.79 |

## 6. Entry Lead Time

Best capital-weighted YES lead bin by ROI among bins with at least 10 closed positions: 24-48h with ROI 153.5% and PnL $15,535.01.

| Weighted Lead | Closed | PnL | ROI | Win |
| --- | --- | --- | --- | --- |
| 0-6h | 111 | $1,324.03 | 85.1% | 48.6% |
| 6-12h | 398 | $6,945.58 | 131.7% | 44.2% |
| 12-24h | 550 | $12,636.87 | 111.8% | 48.4% |
| 24-48h | 505 | $15,535.01 | 153.5% | 53.1% |
| 48-72h | 66 | $1,095.89 | 105.4% | 47.0% |
| 72-168h | 21 | $274.24 | 139.1% | 52.4% |

Lead time uses `weather_date + 1 day at 00:00 UTC` as a public observation-window proxy. Local station cutoffs are not available in the public ledger and remain a data gap.

## 7. Data Gaps and Non-Recoverable Items

- Unfilled orders, cancellations, quote changes, and subjective forecasts are not present in public ledger endpoints.
- Local weather station identity, exact observation cutoff, model forecasts, METAR/TAF snapshots, and alert triggers are not recoverable from the wallet alone.
- Current/open positions are not included in authoritative realized-PnL conclusions.
- Transform-affected SPLIT/MERGE rows are labeled separately; naive cash-flow reconstruction is not trusted for them.
- Overlapping YES markets cannot be treated as mutually exclusive state buckets without an external resolution map.
