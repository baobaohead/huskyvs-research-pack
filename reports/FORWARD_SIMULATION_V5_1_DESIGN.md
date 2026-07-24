# FORWARD_SIMULATION_V5_1_DESIGN

Generated for huskyvs weather-market forward simulation v5.1.

## Purpose

v5.1 is a blocking-fix release for the weather-market forward simulator. It keeps the original v5 files intact and writes only to independent v5.1 paths:

- Formal sample: `data/forward_v5_1/formal/`
- Demo sample: `data/forward_v5_1/demo/`

The simulator never connects to a wallet, never asks for a private key, never signs transactions, and never submits real orders. It only records forward signals, public order-book snapshots, simulated fills, strategy branches, settlements, and audit evidence.

## Core Unit

The primary statistical unit is a city-date-weather-metric event.

`event_key = normalized_city + weather_date_local + normalized_weather_metric`

Temperature buckets, token IDs, condition IDs, and market slugs are not part of the event key. Adjacent temperature bins for the same city, local weather date, and metric are grouped as one event. The 50-event preregistered review threshold counts settled traded events, not raw positions and not demo rows.

## Formal Sample Guardrails

Formal rows can only be written after `start-formal --confirm` has stored:

- formal start time
- configuration hash
- core simulator hash
- reporting script hash
- preregistration document hash

Every formal write path rechecks those hashes. If config, core code, reporting code, or preregistration text drifts, the write is refused and an audit entry is appended. Formal signals are rejected if they are before the formal start time, older than 300 seconds at registration, or more than 30 seconds in the future.

## Signal Flow

Each accepted signal creates an entry order state with:

- intended USD
- filled USD and shares
- remaining USD
- max entry price
- deadline, default 10 minutes after signal creation
- status: `pending`, `partial`, `filled`, `expired`, or `cancelled`

Partial entries stay open until the deadline. Later order-book snapshots may fill the remaining USD. Duplicate order-book snapshots for the same signal do not create duplicate fills.

## Order-Book Execution

Entry simulation uses sell-side depth:

- asks are walked from lowest ask upward
- max entry price is respected
- entry VWAP is calculated across filled levels
- depth shortage records a partial fill

Exit simulation uses buy-side depth:

- bids are walked from highest bid downward
- planned sell shares are filled only to available bid depth
- exit VWAP is calculated across filled levels
- depth shortage keeps the remaining trigger target open

Take-profit eligibility uses executable bid-depth VWAP for the planned shares. It does not use prices-history, page display price, midpoint, last trade, or best bid alone.

## Strategy Branches

Every real simulated entry fill is copied identically into four strategy branches:

- `hold_to_settlement`
- `tp_2x_sell_50pct`
- `tp_2x_sell_75pct`
- `tp_5x_sell_25pct`

Each branch owns independent signal-level FIFO lots. A sell in one strategy never changes the inventory of another strategy.

## One-Time Take-Profit Triggers

For each `signal_id + strategy_id + trigger_stage_id`, v5.1 creates at most one trigger target:

- `trigger_created_at`
- `trigger_target_shares`
- `trigger_filled_shares`
- `trigger_remaining_shares`
- `trigger_status`
- `trigger_completed_at`

If order-book depth only fills part of the target, the unfilled target remains fixed for later snapshots. The target is not recalculated after partial execution, and the same snapshot cannot be counted twice.

## Rolling Cost

Rolling average cost uses only already-filled buy lots at that time:

- open shares
- remaining cost basis, including entry fees
- average cost of open shares

Future add-on signals or later fills cannot change earlier take-profit thresholds. Same-token multi-signal positions are tracked as independent signal lots and then aggregated to the event level.

## Fees

The frozen conservative fee model is:

- entry fee: 10 bps of gross entry notional
- exit fee: 10 bps of gross exit notional
- settlement fee: 0 bps until a confirmed public fee basis is available

Reports preserve gross PnL and net PnL. Net PnL subtracts entry, exit, and settlement fees.

## Settlement

Settlement rows require evidence fields:

- settlement outcome
- settlement value
- source
- source reference
- observed time
- evidence hash
- operator notes

Conflicting settlement values for the same signal and strategy are rejected. Exits after settlement are ignored for settled strategy branches.

## Run Loop

The monitor is foreground-only. It supports status, pause, resume, stop, heartbeat logging, error logging, and a single-instance lock. A network failure or empty/invalid book is logged and does not invent a price.

`--iterations 1` is the safe default. Running indefinitely requires the user to explicitly pass `--iterations 0`, and it still remains a foreground process.

## Integrity Audit

`audit-integrity` checks:

- duplicate signal IDs
- duplicate fill IDs
- duplicate snapshot IDs
- negative inventory
- over-sell
- trigger overfill
- inconsistent strategy entries
- demo rows in formal ledgers
- hash drift
- exits after settlement
- formal registration timeouts

The audit returns `ok: true` only when every blocking check is clean.

