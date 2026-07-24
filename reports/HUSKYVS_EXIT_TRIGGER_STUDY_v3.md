# HUSKYVS_EXIT_TRIGGER_STUDY_v3

Generated at: 2026-07-21T02:59:15.817131+00:00

## Scope

- Selected 42 representative settled weather positions from the corrected v2 sample; no account-level trades were re-fetched.
- Public market price history was queried only for selected asset ids via `https://clob.polymarket.com/prices-history` at 5-minute fidelity.
- Price history source: https://docs.polymarket.com/api-reference/markets/get-prices-history
- Market history available for 42/42 cases. Observed highs combine sampled market history with huskyvs' own public fills, so they are lower bounds on true tick-level highs.

## Sample Mix

| Category | Cases | PnL | Median Sell Multiple | Median Sell Share |
| --- | --- | --- | --- | --- |
| mostly_sold_profit | 12 | $2,240.09 | 5.15 | 100.0% |
| partial_sold_loss | 10 | $-409.38 | 1.01 | 43.6% |
| never_sold_loss | 10 | $-602.01 | n/a | 0.0% |
| prediction_correct_trade_loss | 5 | $-92.02 | 0.69 | 100.0% |
| partial_sold_profit | 5 | $751.98 | 7.90 | 51.6% |

## Required Answers

**Does he often sell after price doubles?** In this representative sample, 15/32 sold cases have weighted sell price at least 2.0x the weighted buy price; 18/32 are at least 1.5x and 13/32 are at least 3.0x. This supports a take-profit behavior around large multiples, but not a single fixed 2x rule.

**Is there a fixed reduction ratio?** Partial-sell cases have median sell-share ratio 45.1%; mostly/fully sold cases have median 100.0%. The ratios are dispersed by design bucket, so no fixed mechanical trim percentage is visible in fills.

**Full sells vs partial sells.**

| Exit Mode | Cases | Actual PnL | Hold-to-Resolution PnL | Median Sell Multiple | Median Sell Share |
| --- | --- | --- | --- | --- | --- |
| mostly_or_fully_sold | 17 | $2,148.07 | $3,715.67 | 3.63 | 100.0% |
| partially_sold | 15 | $342.60 | $-77.69 | 1.36 | 45.1% |
| never_sold | 10 | $-602.01 | $-602.02 | n/a | 0.0% |

**Most effective take-profit multiple or probability zone.** The strongest sampled sell-multiple bucket is `>=3.0x` with total PnL $2,450.86; the strongest entry probability bin is `10-20c` with total sampled PnL $677.80. Treat this as hypothesis generation only because the sample is representative, not exhaustive.

**Signals that help avoid 'correct prediction but losing bet'.** The sample includes 5 cases where the asset ultimately won but realized trading PnL was negative. The common failure mode is selling too much too early or below the blended entry basis before local weather-day end; in contrast, 15 sold cases continued at least 10% above the weighted sell price after the last sell. For the never-sold loss cases, 9/10 had an observed 2x print and 5/10 had an observed 3x print, so a major avoidable failure mode is round-tripping an available profit to settlement loss.

## Candidate Exit Rules To Validate

1. Do not let a correct-weather thesis become a loss by selling the majority below blended cost; require a minimum positive sell multiple before large exits.
2. Treat 1.5x as an alert, 2.0x as the first serious profit-taking threshold, and >=3.0x as the strongest sampled take-profit zone; verify on the full sample before operational use.
3. For tickets that print 2x or 3x and then remain open, force a trim-or-recheck decision before the final local-day window to avoid round-tripping to zero.
4. When the token is likely to settle correct, retain a small residual position instead of fully exiting before local day end unless the price already reflects near-certain settlement.
5. Re-check adjacent basket exposure after a partial sell; a profitable sell on one bucket can leave losing residuals in neighboring buckets.

## Diagnostics

- Low-price never-sold losses in the sample: 3 cases.
- Cases where post-sell price continued up by at least 10%: 15.
- Cases with empty or unavailable public history: 0.

## Data Gaps

- Public CLOB history is sampled, not a full tick/order-book replay; true intraperiod highs and liquidity at size can be missed.
- Open orders, cancellations, queue position, and quote edits remain unavailable from the existing public account files.
- Price-history API availability can vary by resolved market; rows with missing history are explicitly flagged.
- Hold-to-resolution PnL is estimated from v2 settlement state and original bought shares; it ignores whether the same size could have been carried without liquidity or risk constraints.
- The 42-case sample is representative and stratified, not a full-population causal estimate.
