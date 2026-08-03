# Polymarket highest-temperature trader pattern Skill v1

## Scope

This package analyzes only observable public fills made by one or more wallet addresses in Polymarket daily highest-temperature markets. It filters on the market's local weather date and optional canonical cities, then reports each wallet independently plus a cross-wallet comparison.

It does not calculate complete PnL, ROI, win rate, realized or unrealized profit, blockchain profit closure, or Negative Risk conversion economics. It does not connect an account, sign, order, cancel, or use authenticated data. `PUBLIC_DATA_ONLY=true`, `PUBLIC_GET_ONLY=true`, `ACCOUNT_CONNECTION=false`, `SIGNING=false`, `REAL_ORDER=false`, and `FORMAL_STARTED=false` are fixed boundaries.

A public trade record is a fill, not an original order. One order can split into multiple fills; unfilled and cancelled orders are normally absent. Results therefore describe observed executions, not complete order intent or a trader's subjective forecast.

## Inputs and command

- `--wallet` is repeatable. Addresses are lowercased, validated as `0x` plus 40 hexadecimal characters, and deduplicated.
- `--date-from` and `--date-to` are inclusive local weather dates, not trade dates.
- `--city` is repeatable. Case, spaces, underscores, and hyphens normalize to a lowercase hyphenated canonical city. Omitting every city accepts all successfully parsed highest-temperature cities.
- `--city-timezone city=IANA/Zone` explicitly overrides the versioned registry after `zoneinfo` validation.
- Exactly one of `--refresh-public-data` and `--saved-public-evidence-manifest` is required.

Use `--refresh-public-data` for a new wallet or whenever no saved manifest matches the complete requested wallet set and weather-date range. Use `--saved-public-evidence-manifest` only for a matching replay. The bundled manifest in the command below is a legacy Husky-only fixture for `0xaf17116ae2b1476032785a67bd5b7c8c05905c20`; it must never be substituted for another wallet's evidence.

```bash
python -m src.polymarket_highest_temperature_trader_pattern_v1 analyze \
  --wallet 0xaf17116ae2b1476032785a67bd5b7c8c05905c20 \
  --date-from 2026-03-21 \
  --date-to 2026-07-23 \
  --city beijing \
  --output-root /tmp/polymarket_highest_temperature_trader_pattern_v1/run \
  --saved-public-evidence-manifest \
  docs/husky_beijing_full_trade_study_v1/saved_evidence_v1/manifest.json
```

Set `POLYMARKET_PUBLIC_RESEARCH_NO_NETWORK=1` for offline replay only when a matching manifest already exists. Any attempted network request then fails before a request is made. A first run for a new wallet requires the public GET refresh; it creates wallet-specific evidence beneath the chosen output root for later offline replay.

The bundled `.yaml` inputs use the JSON-compatible YAML subset so the runner has no YAML runtime dependency. Environments without a `python` launcher can use their Python interpreter path (for example `python3`) with the same arguments.

## Reused and generalized implementation

The new fixed module imports the protected Beijing study's reviewed `epoch_seconds`, `iso_utc`, `stable_trade_key`, `deduplicate_records`, `Window`, `thirty_day_windows`, and `split_window` primitives. The activity fallback join intentionally generalizes the protected study's `activity_join_key`: it excludes size because `/trades` and `/activity` can represent the same fill with slightly different size values.

The new implementation does not import `HUSKY_WALLET` as a runtime identity, does not use Beijing-specific regular expressions, and does not modify either protected study.

## Market identity

The canonical event form is `highest-temperature-in-<city>-on-<month>-<day>-<year>`. A market slug may append an exact, `or below`, or `or higher` temperature suffix in Celsius or Fahrenheit. The parser cross-checks `eventSlug`, `event_slug`, `slug`, and `title`; it excludes minimum/lowest temperature, rain, forecast, and unrelated markets.

The canonical event slug controls the weather date when present. A contradictory slug or title produces `MARKET_IDENTITY_CONFLICT`, is reported in market discovery/data quality, and is not silently counted. `endDate` does not override an explicit slug date.

## Time and registered buckets

Every fill retains epoch, UTC, Asia/Shanghai, market-local time, IANA timezone, local weather date, relative weather day, and report bucket. `config/highest_temperature_city_timezones_v1.json` is versioned. An unmapped city is retained with blank market-local time and `UNKNOWN` relative day; Asia/Shanghai is never substituted for another city's local time.

Core relative days are exactly `D-2`, `D-1`, and `D0`. D0 is split into `[00:00,08:00)`, `[08:00,12:00)`, `[12:00,16:00)`, and `[16:00,24:00)`. Later fills are `POST_EVENT`. Earlier fills are `EARLIER_THAN_D2`, remain in `all_fills.csv`, enter data quality, and are excluded from core distributions.

## Side, outcome, price, and cumulative shares

`BUY YES`, `BUY NO`, `SELL YES`, and `SELL NO` are separate identities. `implied_yes_equivalent_price = 1 - no_price` is descriptive only and never replaces a NO price or enters YES distributions.

Actual fill prices use exactly:

- `PRICE_0_10C`: `[0.00,0.10)`
- `PRICE_10_30C`: `[0.10,0.30)`
- `PRICE_30_70C`: `[0.30,0.70)`
- `PRICE_70_90C`: `[0.70,0.90)`
- `PRICE_90_100C`: `[0.90,1.00]`

The package does not make a single-fill shares-size distribution. It accumulates shares only for the exact registered key: wallet, city, weather date, event, asset, temperature bucket, bucket kind, outcome, side, canonical exact price, relative weather day, and report time bucket. Cumulative groups use `[0,100)`, `[100,500)`, and `[500,+infinity)`.

Reports display both shares and observed trade USD. `activity.usdcSize` is preferred where an exact public identity match is available; otherwise the documented fallback is price times shares.

## Temperature structures

BUY behavior assigns every event to one exclusive primary class: `SINGLE_YES_TEMPERATURE`, `SINGLE_NO_TEMPERATURE`, `MULTI_YES_ONLY`, `MULTI_NO_ONLY`, `MIXED_YES_NO`, or `NO_BUY`. Mixed events additionally use `SAME_BUCKET_BOTH_SIDES`, `CROSS_BUCKET_YES_NO`, or `BOTH`. Exact same-unit temperatures one degree apart are adjacent; tail buckets are never treated as ordinary adjacent exact temperatures.

## Outputs and evidence

Each wallet gets all required CSV, JSON, and Markdown files under its own directory. `market_discovery.csv` has one row per discovered contract identity, not one row per fill. Distribution files retain `POST_EVENT` and `UNKNOWN` rows so their totals reconcile to summaries, but exclude `EARLIER_THAN_D2` from the registered strategy distributions. The run root gets `trader_comparison.csv`, `trader_comparison.md`, and `run_manifest.json`. Actual report outputs and raw API evidence belong under the caller's output root and must not be committed.

Live evidence records the GET base URL, parameters, request time, record count, SHA256, success/failure, retries, and relative raw-response path. Offline manifests reject absolute paths and `..`, verify SHA256 and record count, enforce wallet and date range, and verify the public-only safety flags. Pagination saturation is reported as `PAGINATION_INCOMPLETE`; it is never silently treated as complete.

## Validation

The new program and skill suites verify arbitrary-wallet routing as well as manifest wallet isolation. The Husky portable replay preserves 50 events, 537 fills, 453 BUY, 84 SELL, 400 BUY YES, 53 BUY NO, 29 multi-YES events, and 21 events with adjacent YES buckets. The reviewed/new fill sets are compared on transaction hash, condition ID, asset, side, normalized outcome, price, shares, and timestamp, with the wallet fixed to the requested Husky address.
