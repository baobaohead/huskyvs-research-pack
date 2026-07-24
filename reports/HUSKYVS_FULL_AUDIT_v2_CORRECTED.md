# HUSKYVS_FULL_AUDIT_v2_CORRECTED

Generated at: 2026-07-21T02:26:58.090512+00:00
Wallet: `0xaf17116ae2b1476032785a67bd5b7c8c05905c20`

This v2 audit uses existing `data/raw` only. No public data was re-fetched.

## Corrections Applied

- Added `1566` weather assets from `current_positions` where the local weather day had ended, `currentValue=0`, and `cashPnl<0`, deduplicated by `asset` against `closed_positions`.
- Current-position additions use `realizedPnl + cashPnl` as full asset economic PnL, while preserving both components in the lifecycle CSV.
- Entry lead time now uses each city's local timezone and local weather-day end, not a single UTC proxy.
- Exit modes are `never_sold`, `partially_sold`, `mostly_or_fully_sold`, and `transform_affected`; sell timing is checked against local weather-day end.

## Headline Numbers

- Settled weather assets v2: 3645
- Total weather net PnL v2: $22,842.55
- ROI on capital at risk v2: 32.3%
- Max drawdown v2: $843.98
- Top1/Top5/Top10 position profit share of net: 4.3% / 16.2% / 27.1%

## Required Answers

**Does huskyvs rely on holding low-price tickets to resolution?** Low-price YES `never_sold` groups from 0-10c sum to $-4,931.59. The corrected sample does not support the claim that low-price tickets held to resolution are the profit engine; this group is net negative after adding current-position losses.

**Does profit mainly come from pre-resolution selling?** `partially_sold + mostly_or_fully_sold` positions sum to $25,853.70, while `never_sold` sums to $-5,463.38. On this corrected sample, sell-involved positions contribute more net profit than never-sold positions.

**Best local entry window?** Among YES bins with at least 10 settled positions, the best capital-weighted local lead bin is `72h+` with ROI 152.2% and PnL $275.89.

**Does multi-city diversification still hold?** Pairwise city correlations use overlap days only, with no blank-date zero fill. Average correlation is 0.011 across 331 pairs, so diversification still appears meaningful, though large winners still affect net PnL.

**Is the actual adjacent-basket sizing better than simple equal-amount sizing?** Across 591 evaluable adjacent exact/range YES baskets, actual trading PnL is $14,977.80, original-shares hold-to-resolution is $14,118.75, and equal-amount same-input PnL is $13,434.79. In aggregate, the actual configuration beats the simple equal-amount counterfactual in this corrected state model.

Counterfactual limits: all basket comparisons assume fills were available at the observed weighted average prices and ignore non-simultaneous execution, market impact, queue priority, and changing information over time.

## Exit Mode PnL

| Mode | Settled | PnL | ROI | Sold Before Local End |
| --- | --- | --- | --- | --- |
| never_sold | 1898 | $-5,463.38 | -23.2% | 0 |
| partially_sold | 319 | $-827.58 | -8.1% | 319 |
| mostly_or_fully_sold | 1272 | $26,681.28 | 96.1% | 1260 |
| transform_affected | 156 | $2,452.23 | 26.8% | 62 |

## YES Price Bins by Exit Mode

| Price | Exit | Settled | PnL | ROI |
| --- | --- | --- | --- | --- |
| 0-1c | mostly_or_fully_sold | 52 | $1,672.64 | 995.9% |
| 0-1c | never_sold | 324 | $-239.36 | -29.3% |
| 0-1c | partially_sold | 17 | $-8.68 | -8.7% |
| 0-1c | transform_affected | 3 | $515.60 | 11292.5% |
| 1-2c | mostly_or_fully_sold | 68 | $474.41 | 108.7% |
| 1-2c | never_sold | 254 | $-1,034.40 | -92.7% |
| 1-2c | partially_sold | 30 | $-53.67 | -14.4% |
| 1-2c | transform_affected | 36 | $2,614.87 | 4749.5% |
| 2-5c | mostly_or_fully_sold | 144 | $5,571.88 | 448.2% |
| 2-5c | never_sold | 354 | $-1,659.99 | -68.3% |
| 2-5c | partially_sold | 62 | $-260.61 | -28.3% |
| 2-5c | transform_affected | 16 | $1,220.35 | 853.2% |
| 5-10c | mostly_or_fully_sold | 192 | $4,860.78 | 208.5% |
| 5-10c | never_sold | 279 | $-1,997.84 | -58.8% |
| 5-10c | partially_sold | 80 | $55.12 | 2.5% |
| 5-10c | transform_affected | 2 | $218.70 | 610.9% |
| 10-20c | mostly_or_fully_sold | 291 | $7,844.43 | 153.8% |
| 10-20c | never_sold | 314 | $-878.91 | -17.0% |
| 10-20c | partially_sold | 77 | $-604.78 | -22.3% |
| 10-20c | transform_affected | 6 | $219.86 | 146.3% |
| >=20c | mostly_or_fully_sold | 300 | $5,392.32 | 52.7% |
| >=20c | never_sold | 162 | $-834.43 | -16.1% |
| >=20c | partially_sold | 22 | $-9.98 | -0.4% |
| >=20c | transform_affected | 15 | $573.94 | 46.7% |

## V1 vs V2

| Metric | V1 | V2 | Delta |
| --- | --- | --- | --- |
| settled_weather_assets | 2,079.0000 | 3,645.0000 | 1,566.0000 |
| total_net_pnl | 37,894.3606 | 22,842.5522 | -15,051.8084 |
| roi_on_capital_at_risk | 0.7438 | 0.3230 | -0.4208 |
| max_drawdown_usd | 371.0115 | 843.9784 | 472.9669 |
| top1_share_of_net_pnl | 0.0256 | 0.0425 | 0.0169 |
| top5_share_of_net_pnl | 0.0974 | 0.1616 | 0.0642 |
| top10_share_of_net_pnl | 0.1633 | 0.2708 | 0.1076 |
| current_zero_value_added_positions | 0.0000 | 1,566.0000 | 1,566.0000 |
| current_zero_value_added_pnl | 0.0000 | -15,051.8084 | -15,051.8084 |

## Remaining Data Gaps

- Open orders, cancellations, quote edits, and queue position are not recoverable from public ledger files.
- Counterfactual basket results cannot prove executable performance because liquidity and timing are not reconstructed.
- Some `current_positions` rows may represent residual token accounting rather than complete market-level lifecycle; v2 preserves source labels for audit.
- Exact weather-station observation cutoffs are approximated as local calendar-day end; market-specific resolution delays are not reconstructed.
- Correlation estimates use only overlapping active city-days; they do not model weather-regime common shocks outside the traded sample.
