---
name: polymarket-highest-temperature-trader-pattern-v1
description: Analyze one or more wallet addresses' observable public fill patterns in Polymarket daily highest-temperature markets over a local weather-date range, optionally filtering cities or comparing traders. Use for YES/NO, BUY/SELL, price-band, local-time, same-price cumulative-shares, temperature-combination, city, or multi-trader pattern reports. Do not use for PnL, ROI, blockchain profit closure, real trading, unfilled orders, other market types, or Negative Risk conversion economics.
---

# Analyze highest-temperature trader patterns

Use the bundled deterministic runner. Do not recreate or alter its registered buckets.

## Workflow

1. Collect one or more wallet addresses, `date_from`, `date_to`, and optional cities. Treat an omitted or empty city list as all identifiable cities.
2. Validate every wallet as a 42-character `0x` address. Normalize to lowercase and deduplicate.
3. Choose exactly one evidence mode:
   - Use `--refresh-public-data` for official unauthenticated Polymarket GET collection.
   - Use `--saved-public-evidence-manifest PATH` for validated offline replay.
4. Run:

```bash
python skills/polymarket-highest-temperature-trader-pattern-v1/scripts/run_analysis.py \
  --input INPUT.yaml \
  --output-root OUTPUT_DIRECTORY \
  --saved-public-evidence-manifest MANIFEST.json
```

Use JSON-compatible YAML for the bundled input file; this keeps the runner dependency-free. Use the module CLI directly when a `python` launcher or general YAML parser is unavailable.

5. Check each wallet's `data_quality.csv`. Call out `PAGINATION_INCOMPLETE`, request failures, unknown timezones, unknown relative days, identity conflicts, and invalid fills.
6. Read each wallet's `summary.json` and the root `trader_comparison.csv`/`.md`.
7. Return plain-language findings, the output directory, and the evidence limitations.

## Guardrails

- Describe public fills, not original order intent. One order can produce multiple fills; cancelled and unfilled orders are normally invisible.
- Keep BUY YES, BUY NO, SELL YES, and SELL NO separate. Never treat NO price or `1 - NO price` as a YES fill price.
- Report both shares and actual observed trade USD. Do not describe cheap high-share fills as large capital unless USD is also large.
- Determine D-2, D-1, D0, and D0 hour buckets in the market city's registered local timezone. Retain an unknown-timezone fill but label its local time and relative day `UNKNOWN`.
- Exclude `EARLIER_THAN_D2` from core strategy distributions and report it as a data-quality condition.
- Do not output complete PnL, ROI, win rate, realized/unrealized/on-chain profit, profitability rankings, subjective intent, or claims that a strategy is profitable.
- Never connect an account, request credentials, sign, order, cancel, POST, PUT, PATCH, or DELETE.

The fixed implementation is `src/polymarket_highest_temperature_trader_pattern_v1.py`. The bundled runner only translates the input file into that module's command-line interface.
