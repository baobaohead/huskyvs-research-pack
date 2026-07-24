# FINAL_DELIVERY_SUMMARY

## Row Counts

- `trades`: 18,308
- `activity`: 19,498
- Weather trades parsed: 17,964
- Weather assets: 3,663
- Closed weather assets with authoritative `realizedPnl`: 2,079

## Pytest Result

- Command: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider`
- Result: `3 passed in 0.02s`

## Data Integrity Audit

- `trades` request used `takerOnly=false`: passed
- `activity` contains `TRADE`, `SPLIT`, `MERGE`, and `REDEEM`: passed
- CSV, JSONL, and manifest row counts are consistent: passed
- Offset truncation audit: passed
- Accounting snapshot ZIP validity: passed in the original raw audit, but the ZIP is intentionally excluded from this delivery package
- Duplicate audit: `trades` has no duplicate groups under `transactionHash/asset/side/timestamp/size/price`; `activity` duplicate groups are `REDEEM` rows with blank asset/side/price and were not treated as duplicated fills
- Future information leakage audit: observed winners are used only for resolved counterfactuals; entry lead time uses a weather-date-end UTC proxy

## Generated File List

- `reports/HUSKYVS_FULL_AUDIT_v1.md`
- `data/raw/manifest.json`
- `data/processed/audit_summary.json`
- `data/processed/basket_state_payoffs.csv`
- `data/processed/city_correlation.csv`
- `data/processed/city_day_pnl.csv`
- `data/processed/closed_positions_weather_normalized.csv`
- `data/processed/counterfactual_basket_summary.csv`
- `data/processed/current_positions_weather_normalized.csv`
- `data/processed/data_integrity_audit.json`
- `data/processed/entry_lead_time.csv`
- `data/processed/entry_lead_time_by_bin.csv`
- `data/processed/portfolio_daily_pnl.csv`
- `data/processed/price_bin_by_exit_mode.csv`
- `data/processed/profit_concentration.csv`
- `data/processed/weather_city_day_baskets.csv`
- `data/processed/weather_position_lifecycle.csv`
- `data/processed/weather_trades_normalized.csv`
- `src/analyze_weather_strategy.py`
- `src/collect_public_ledger.py`
- `tests/test_parser.py`
- `FINAL_DELIVERY_SUMMARY.md`

## Known Data Gaps

- Public endpoints do not recover unfilled orders, cancellations, quote changes, or subjective forecast notes.
- Local weather station identity, exact observation cutoff, model forecast snapshots, METAR/TAF snapshots, and alert triggers are not recoverable from the wallet ledger alone.
- Open/current positions are excluded from authoritative realized-PnL conclusions.
- SPLIT/MERGE transform-affected rows are labeled separately; naive cash-flow reconstruction is not trusted for them.
- Overlapping YES markets such as `or below` and `or higher` cannot be treated as mutually exclusive state buckets without an external resolution map.

## Manual Review Items Still Required

1. Whether the `24-48h` best entry window is materially affected by the UTC proxy observation cutoff.
2. Whether the city correlation value of `0.033` is distorted by date alignment choices, including any zero-fill treatment for blank dates.
3. Whether the `153.5%` ROI figure is affected by survivorship bias because the headline entry-lead analysis uses closed positions with authoritative realized PnL.
