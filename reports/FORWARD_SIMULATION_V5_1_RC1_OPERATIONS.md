# FORWARD_SIMULATION_V5_1_RC1_OPERATIONS

## Version To Use

Use the RC1 fixed files:

```bash
PYTHONPATH=. python3 -m src.forward_simulation_v5_1_1 --config config/forward_simulation_v5_1_1.yaml --root /Users/baobaotou/Documents/竞争对手分析/huskyvs_research_pack status --mode formal
```

Do not formally start the old v5.1 CSV/JSONL ledger.

## Initialize A Ledger

```bash
PYTHONPATH=. python3 -m src.forward_simulation_v5_1_1 --config config/forward_simulation_v5_1_1.yaml --root /Users/baobaotou/Documents/竞争对手分析/huskyvs_research_pack init --mode formal
```

Initialization does not start the formal sample.

## Formal Start Command

Only after explicit user approval:

```bash
PYTHONPATH=. python3 -m src.forward_simulation_v5_1_1 --config config/forward_simulation_v5_1_1.yaml --root /Users/baobaotou/Documents/竞争对手分析/huskyvs_research_pack start-formal --confirm
```

This records the formal start time and hashes for config, core code, reporting code, schema, and preregistration.

## Register Signals

Prepare a CSV with the standard signal fields and run:

```bash
PYTHONPATH=. python3 -m src.forward_simulation_v5_1_1 --config config/forward_simulation_v5_1_1.yaml --root /Users/baobaotou/Documents/竞争对手分析/huskyvs_research_pack register --signals-file path/to/signals.csv --mode formal
```

The system creates `registered_at_utc`. Do not supply it manually.

## Monitoring

RC1 tests are offline and do not access Polymarket. Before live formal monitoring, the public order-book adapter must be confirmed and reviewed against the same shared-depth allocation rules.

The run-loop design is foreground-only. It does not create cron, launchd, daemon, background service, wallet connection, or real order submission.

## Audit

Run:

```bash
PYTHONPATH=. python3 -m src.forward_simulation_v5_1_1 --config config/forward_simulation_v5_1_1.yaml --root /Users/baobaotou/Documents/竞争对手分析/huskyvs_research_pack audit-integrity --mode formal
```

`ok: true` is required before any review.

