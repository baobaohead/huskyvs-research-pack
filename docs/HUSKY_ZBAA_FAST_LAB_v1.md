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

## Evidence time versus processing time

Every run keeps four different clocks:

- `weather_as_of_time_utc` is the weather-information cutoff: D-1 15:00 CST
  (07:00 UTC).
- `forecast_generated_at_utc` is when the manual probability calculation
  finished. It may be from 15:00 through 15:05 CST, inclusive.
- `market_captured_at_utc` is when that specific order book was actually
  received or saved.
- `decision_created_at_utc` is when the program calculated the decision and
  wrote the report.

The first time limits what weather information may enter the probability. The
second time only records when that frozen-input probability was completed;
generation after 15:00 does not by itself mean weather leakage. Generation
before 15:00 is rejected as `GENERATED_BEFORE_CUTOFF`, and generation after
15:05 is rejected as `GENERATED_AFTER_OUTPUT_WINDOW`.

Signals and entry fills keep the weather cutoff, market capture, and decision
processing times separately. The manifest and decision report explicitly keep
all four meanings and show the earliest and latest entry evidence times.
`market_snapshot.json` uses `snapshot_written_at_utc` for the file-writing time
and keeps each market's real capture time; it never relabels the current clock
as market evidence time.

In plain language: the market evidence time determines where the shadow trade
sits on the historical timeline. The processing time only says when the
command was run. Saved evidence keeps its saved timestamp even when replayed
days or years later. Live-readonly evidence uses the HTTP order-book response's
`received_at_utc`.

Every evidence record must have a non-empty, valid ISO-8601 timestamp with an
explicit UTC timezone. Missing, timezone-naive, non-UTC, or fabricated fallback
times are rejected.

For entry only, every saved or live-readonly order book must fall within D-1
15:00 CST ±60 seconds (06:59:00Z through 07:01:00Z, inclusive). One market
outside that fixed window rejects the complete run before an output directory
or ledger is created, using `ENTRY_MARKET_BEFORE_CUTOFF_WINDOW` or
`ENTRY_MARKET_AFTER_CUTOFF_WINDOW`. This entry window is not applied to later
`update-shadow` evidence; updates retain their strict rules of being later than
entry and later than the previous update.

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

An update records both `evidence_captured_at_utc` (the order-book evidence
time) and `update_processed_at_utc` (when the command ran). If an exit is
triggered, `trigger_time_utc` equals the evidence time, not the processing
time. Per token, update evidence must be strictly later than entry evidence
and strictly later than the previous update evidence. Reusing the same
evidence is rejected as `STALE_OR_REPEATED_EVIDENCE`; going backward is
rejected as `OUT_OF_ORDER_EVIDENCE`. These checks happen before any ledger
change, so a rejected update cannot partially sell or append snapshots. A
fresh later update after an already-triggered exit remains
`REPEATED_EXIT_REJECTED` and cannot sell the position twice.

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
