---
name: polymarket-highest-temperature-trader-pattern-v1
description: Analyze one or more wallets in Polymarket daily highest-temperature markets over a local weather-date range, optionally filtering cities or comparing traders. Use basic or advanced for observable YES/NO and BUY/SELL public-fill patterns, profitability for official hybrid position-PnL summaries on fully resolved target markets, and full to combine those independent outputs. Do not use for ROI, full blockchain ledgers, strategy PnL attribution, real trading, unfilled orders, other market types, or Negative Risk conversion economics.
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
8. Choose the analysis depth. The default is `basic`; preserve the basic files and statistics exactly.
   - Use `advanced` for “深度分析”, “高级交易模式分析”, or “分析逐资产交易路径”.
   - Use `profitability` for PnL, 盈利能力, 累计盈亏, 日度/月度盈亏, 盈利日、亏损日、连续盈亏, 盈利集中度, or 稳定性 requests.
   - Use `full` when the user asks for “完整分析” or explicitly wants basic + advanced + profitability. `full` is a thin composition and never recalculates a component.
9. For `advanced`, reuse `src/polymarket_highest_temperature_trader_pattern_advanced.py`. It adds per-wallet `advanced_summary.md`, `advanced_summary.json`, `asset_path_summary.csv`, `high_sell_path_fills.csv`, `high_sell_path_assets.csv`, `daily_temperature_structure.csv`, and `trader_style_metrics.csv`, plus root `advanced_trader_comparison.md` and `.json`. Do not overwrite the basic `summary.*` or basic CSVs.
10. For `profitability`, reuse `src/polymarket_highest_temperature_trader_profitability.py`. It obtains official unauthenticated `GET /v1/market-positions` and `GET /closed-positions` records for every target condition, reconciles them by the unique `(conditionId, asset, outcome)` position key, and uses the canonical hybrid rule: market-position `totalPnl` when present; closed-position `realizedPnl` only when the market-position source is absent; BOTH uses market total once and never adds the sources. It verifies market `totalPnl = cashPnl + realizedPnl` within the fixed tolerance and writes per-wallet `profitability_summary.md`/`.json`, `daily_profitability.csv`, `monthly_profitability.csv`, `profitability_data_quality.csv`, and `event_profitability_audit.csv`, plus root profitability comparison files.
    - Reuse a matching `--saved-profitability-evidence-manifest PATH` when available. If saved evidence lacks v1.1 hybrid position evidence or final-resolution evidence and network is allowed, perform only the minimal official source/resolution refresh into `_profitability_public_evidence`; if network is disabled, emit `PROFITABILITY_STATUS=BLOCKED`; never infer PnL from fills.
    - Preserve market `cashPnl` and `realizedPnl` as diagnostics, and preserve closed-position `realizedPnl` both as the explicit fallback source for CLOSED_ONLY positions and as a cross-source audit field for BOTH positions. A BOTH-source mismatch beyond tolerance is a fail-closed conflict; never sum market total and closed realized.
    - When advanced READY public-fill evidence is available, report observed-traded-position coverage separately: every observed `(conditionId, asset, outcome)` must have at least one official source. If advanced evidence is unavailable, set coverage to `NOT_AUDITABLE_WITH_PATTERN_EVIDENCE` without blocking an otherwise independent profitability run.
    - Keep arch/new duplicate events separate in event audit, but aggregate daily and monthly PnL by `canonical_city + weather_date_local`; derive the month from that weather date.
    - Keep a not-yet-finally-resolved boundary event in event audit with `settlement_status=NOT_RESOLVED`, exclude it from settled PnL and profit/loss/flat days, and report `SETTLED_SCOPE_END`, `UNSETTLED_BOUNDARY_COUNT`, and boundary dates/events. This condition alone does not lower `PROFITABILITY_STATUS` from `READY` when every resolved event is complete.
    - Read `PROFITABILITY_STATUS` independently from `pattern_report_status`. `PARTIAL` means affected events are isolated and excluded; `BLOCKED` means no aggregate PnL conclusion is safe.
11. For `full`, read the component files and `full_trader_report.md`/`.json`. If advanced is blocked but profitability is ready, report the two statuses separately and retain the reliable profitability result.
12. Return plain-language findings, the output directory, and the evidence limitations.

## Guardrails

- Describe public fills, not original order intent. One order can produce multiple fills; cancelled and unfilled orders are normally invisible.
- Keep BUY YES, BUY NO, SELL YES, and SELL NO separate. Never treat NO price or `1 - NO price` as a YES fill price.
- Report both shares and actual observed trade USD. Do not describe cheap high-share fills as large capital unless USD is also large.
- Determine D-2, D-1, D0, and D0 hour buckets in the market city's registered local timezone. Retain an unknown-timezone fill but label its local time and relative day `UNKNOWN`.
- Exclude `EARLIER_THAN_D2` from core strategy distributions and report it as a data-quality condition.
- In `basic` and `advanced`, do not output PnL, ROI, win rate, realized/unrealized/on-chain profit, profitability rankings, or claims that a strategy is profitable.
- In `profitability` and the profitability component of `full`, report only the official hybrid position PnL for fully resolved target markets: market-position `totalPnl` primary, closed-position `realizedPnl` fallback for CLOSED_ONLY, and no double count for BOTH. Retain cash/realized diagnostics and source classes. Do not reconstruct a full ledger, compute ROI, annualized return, Sharpe, gas, rebates, unrealized PnL, wallet flows, or Negative Risk split/merge/conversion economics. Do not attribute PnL to a fill pattern or strategy.
- Treat the profitability stability label as an internal Skill research-comparison rating, not an industry performance rating or investment recommendation.
- Never connect an account, request credentials, sign, order, cancel, POST, PUT, PATCH, or DELETE.
- Without maker/taker fields, never assign `POSSIBLE_MARKET_MAKER`; at most emit `MARKET_MAKER_LIKE_ACTIVITY=true` with the Chinese caveat that the behavior resembles high-frequency two-way trading but maker/taker evidence is unavailable.

The fixed router/basic implementation is `src/polymarket_highest_temperature_trader_pattern_v1.py`; advanced and profitability calculations live only in their named sibling modules. The bundled runner only translates the input file into that command-line interface.
