---
name: polymarket-highest-temperature-trader-pattern-v1
description: Analyze one or more wallet addresses' observable public fill patterns in Polymarket daily highest-temperature markets over a local weather-date range, optionally filtering cities or comparing traders. Use for YES/NO, BUY/SELL, price-band, local-time, same-price cumulative-shares, temperature-combination, city, or multi-trader pattern reports. Do not use for PnL, ROI, blockchain profit closure, real trading, unfilled orders, other market types, or Negative Risk conversion economics.
---

# Analyze highest-temperature trader patterns

Use the bundled deterministic runner. Do not recreate or alter its registered buckets.

## Workflow

1. Collect one or more wallet addresses, `date_from`, `date_to`, and optional cities. Treat an omitted or empty city list as all identifiable cities.
2. Validate every wallet as a 42-character `0x` address. Normalize to lowercase and deduplicate.
3. Choose exactly one evidence mode based on the requested wallet set:
   - For a new wallet, or whenever no saved manifest matches every requested wallet and the weather-date range, use `--refresh-public-data`. This uses only official unauthenticated Polymarket GET endpoints and creates wallet-specific evidence for later replay.
   - Use `--saved-public-evidence-manifest PATH` only after confirming that the manifest belongs to every requested wallet and matches the weather-date range. Never substitute evidence from another wallet.
   - The repository's bundled `docs/husky_beijing_full_trade_study_v1/saved_evidence_v1/manifest.json` belongs only to `0xaf17116ae2b1476032785a67bd5b7c8c05905c20`; it cannot analyze any other wallet.
   - If the user requires no network and no matching manifest exists, explain that a first public-data refresh is required. Do not create a `BLOCKED.md` or modify the repository merely to record this condition.
4. For a matching offline manifest, run:

```bash
python skills/polymarket-highest-temperature-trader-pattern-v1/scripts/run_analysis.py \
  --input INPUT.yaml \
  --output-root OUTPUT_DIRECTORY \
  --saved-public-evidence-manifest MANIFEST.json
```

Use JSON-compatible YAML for the bundled input file; this keeps the runner dependency-free. Use the module CLI directly when a `python` launcher or general YAML parser is unavailable.

For a new wallet, replace the saved-manifest argument with `--refresh-public-data`. The refresh first discovers the target highest-temperature events and condition IDs from the official Gamma API, then queries the wallet by each target condition through both official `/activity` and `/trades` GET endpoints. It keeps an audited, deduplicated union with `source_activity`, `source_trades`, or `source_both` provenance. The run saves a wallet-specific manifest beneath `OUTPUT_DIRECTORY/_public_evidence/`; use that generated manifest for later offline replays of the same wallet set and weather-date range.

## City scope

- Omit `cities` or pass an empty list to discover and analyze every recognizable Polymarket daily highest-temperature city in the requested weather-date range. Target-market discovery is independent of whether the wallet has a fill. This is not limited to Beijing or Shanghai.
- Pass one or more canonical market city slugs, such as `beijing`, `nyc`, `hong-kong`, or `cape-town`, to restrict the report to those cities.
- Use the bundled IANA registry for the verified Polymarket city set. Retain a future unregistered city but mark its market-local time and relative day `UNKNOWN`; use `city_timezones` overrides only with a verified `city=IANA/Zone` value.
- Accept current year-qualified event slugs and legacy yearless daily event slugs. Keep exact, range, `or below`, and `or higher` temperature contracts distinct.

5. Check each wallet's `data_quality.csv`. Call out target event/condition coverage, per-market completeness, source-only fills, orphan sells, `PAGINATION_INCOMPLETE`, request failures, unknown timezones, unknown relative days, identity conflicts, and invalid fills.
6. Read `pattern_report_status` before reporting any pattern. If it is `BLOCKED_INCOMPLETE_EVIDENCE`, report that the evidence is incomplete and the pattern analysis is paused; do not state main times, price preferences, or temperature-combination conclusions from the partial data.
7. If `pattern_report_status` is `READY`, read each wallet's `summary.json` and the root `trader_comparison.csv`/`.md`.
8. Choose the analysis depth. The default is `basic`; preserve the basic files and statistics exactly. Use `advanced` when the user passes `--analysis-depth advanced`, sets `analysis_depth: advanced` in the JSON-compatible input, or asks for “深度分析”, “高级交易模式分析”, or “分析逐资产交易路径”.
9. For `advanced`, reuse the formal advanced core in `src/polymarket_highest_temperature_trader_pattern_advanced.py`. It adds per-wallet `advanced_summary.md`, `advanced_summary.json`, `asset_path_summary.csv`, `high_sell_path_fills.csv`, `high_sell_path_assets.csv`, `daily_temperature_structure.csv`, and `trader_style_metrics.csv`, plus root `advanced_trader_comparison.md` and `.json`. Do not overwrite the basic `summary.*` or basic CSVs.
10. Return plain-language findings, the output directory, and the evidence limitations.

## Guardrails

- Describe public fills, not original order intent. One order can produce multiple fills; cancelled and unfilled orders are normally invisible.
- Keep BUY YES, BUY NO, SELL YES, and SELL NO separate. Never treat NO price or `1 - NO price` as a YES fill price.
- Report both shares and actual observed trade USD. Do not describe cheap high-share fills as large capital unless USD is also large.
- Determine D-2, D-1, D0, and D0 hour buckets in the market city's registered local timezone. Retain an unknown-timezone fill but label its local time and relative day `UNKNOWN`.
- Exclude `EARLIER_THAN_D2` from core strategy distributions and report it as a data-quality condition.
- Do not output complete PnL, ROI, win rate, realized/unrealized/on-chain profit, profitability rankings, subjective intent, or claims that a strategy is profitable.
- Never connect an account, request credentials, sign, order, cancel, POST, PUT, PATCH, or DELETE.
- Without maker/taker fields, never assign `POSSIBLE_MARKET_MAKER`; at most emit `MARKET_MAKER_LIKE_ACTIVITY=true` with the Chinese caveat that the behavior resembles high-frequency two-way trading but maker/taker evidence is unavailable.

The fixed implementation is `src/polymarket_highest_temperature_trader_pattern_v1.py`. The bundled runner only translates the input file into that module's command-line interface.
