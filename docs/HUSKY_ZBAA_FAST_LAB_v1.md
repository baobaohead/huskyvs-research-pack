# Husky ZBAA Fast Strategy Lab v1

This is a narrow shadow-simulation tool for one station (`ZBAA`), one city
(`Beijing`), one metric (`highest_temperature`), and one manual forecast mode
(`SHADOW_MANUAL`). It never starts formal mode, connects an account or wallet,
signs, or sends a real order.

## Commands

Analyze recorded Husky behavior without changing the source data:

```bash
python3 -m src.husky_zbaa_fast_lab_v1 analyze-history \
  --output docs/HUSKY_STRATEGY_EVIDENCE_FAST_v1.md
```

Run a saved-evidence DEMO:

```bash
python3 -m src.husky_zbaa_fast_lab_v1 run-shadow \
  --probability-input templates/zbaa_shadow_probability_input_v1.json \
  --intended-usd 20 \
  --output-dir /tmp/husky_zbaa_fast_lab_v1/demo \
  --saved-public-evidence tests/fixtures/zbaa_fast_lab_saved_evidence_v1.json
```

Run a public-GET-only probe after replacing the template with a genuine user
D-1 forecast for tomorrow:

```bash
python3 -m src.husky_zbaa_fast_lab_v1 run-shadow \
  --probability-input /path/to/user_zbaa_probability.json \
  --intended-usd 20 \
  --output-dir /tmp/husky_zbaa_fast_lab_v1/live_readonly \
  --live-readonly
```

The command rejects `--mode FORMAL`, any station other than ZBAA, non-Beijing
city aliases, non-high-temperature metrics, non-D-1 dates, timestamps other
than 15:00 CST / 07:00 UTC, post-cutoff generation, incomplete or duplicate
integer temperature ranges, and probability sums other than one.

## Fixed experiment matrix

- Entry edges: `EDGE_05`, `EDGE_10`, `EDGE_15`.
- Portfolio forms: `MAIN_ONLY`; `TOP2_ADJACENT` at 70% / 30%.
- Exit controls: `HOLD`, `DOUBLE_SELL_50`, `DOUBLE_SELL_75`.
- Baseline: `NO_TRADE`.

Every entry edge uses the manual bucket probability minus the volume-weighted
average price obtainable from visible asks at the requested amount. Best ask
is reported but is not used as a substitute for executable average price.

Each run writes `decision_report.json`, `decision_report.md`,
`shadow_signals.csv`, `market_snapshot.json`, `orderbook_snapshots/`,
`run_manifest.json`, and an isolated `demo_ledger.sqlite3`.

## Stable run identity

`run_id` is deterministically bound to `forecast_run_id`, station,
`weather_date_local`, `as_of_time_utc`, and the SHA-256 of the complete
normalized probability input. Every `signal_id` is deterministically bound to
that `run_id`, edge rule, portfolio rule, and temperature bucket.

An exact rerun returns `IDEMPOTENT_NOOP` without touching the ledger. The tool
rejects a changed input under the same forecast identity, an output directory
owned by another run, an incomplete pre-existing run, and any attempt to
silently replace an existing ledger row.

## Update open shadow positions

Use saved public evidence:

```bash
python3 -m src.husky_zbaa_fast_lab_v1 update-shadow \
  --run-dir /tmp/husky_zbaa_fast_lab_v1/run-2026-07-22 \
  --saved-public-evidence /path/to/later_public_evidence.json
```

Or replace the evidence argument with `--live-readonly`. Only tokens already
bound to the selected run are evaluated. Exit simulation consumes bid depth.
The 50% or 75% target is sold only when the complete target quantity has an
executable bid-side VWAP at least twice its entry VWAP. Best bid alone never
triggers an exit. HOLD remains open. Every update adds a new immutable snapshot
file and ledger snapshot row.

## Settle and summarize

Settle one run with the observed integer maximum:

```bash
python3 -m src.husky_zbaa_fast_lab_v1 settle-shadow \
  --run-dir /tmp/husky_zbaa_fast_lab_v1/run-2026-07-22 \
  --observed-max-temp-c 28
```

An identical settlement returns `IDEMPOTENT_NOOP`; a different repeated
temperature is rejected. Exact, lower-tail (`or_below`), and upper-tail
(`or_higher`) buckets are supported. Each exit experiment reports invested
USD, realized exit proceeds, remaining-share settlement proceeds, total
proceeds, PnL, and ROI.

Summarize settled events:

```bash
python3 -m src.husky_zbaa_fast_lab_v1 summarize-shadow \
  --runs-root /tmp/husky_zbaa_fast_lab_v1/
```

Only settled runs are included. A station/date is counted once, each fixed
edge/portfolio/exit combination is reported, and NO_TRADE is included as a
zero-position comparator. Samples below 30 settled weather events are labeled
`INSUFFICIENT_FORWARD_SAMPLE`.

## Evidence labels

The repository fixture is synthetic and labeled
`ZBAA_FAST_LAB_TEST_FIXTURE_NOT_LIVE`. Its report must never be described as a
live opportunity. A live-readonly run records public endpoint URLs and capture
times, and it remains a DEMO simulation.
