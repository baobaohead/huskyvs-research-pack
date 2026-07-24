# FORWARD_SIMULATION_V5_PREREGISTRATION

## Status

Formal monitoring has not been started. The formal sample begins only when the user explicitly runs the start command.

Formal start command:

```bash
cd /Users/baobaotou/Documents/竞争对手分析/huskyvs_research_pack
PYTHONPATH=. python3 -m src.forward_simulation_v5 --root . --config config/forward_simulation_v5.yaml start-formal --confirm
```

## Frozen Rules

These four rules are pre-registered and must not be changed based on interim results:

1. `hold_to_settlement`: completely hold to settlement.
2. `tp_2x_sell_50pct`: sell 50% of current remaining inventory when executable bid-depth VWAP is at least 2x rolling average cost.
3. `tp_2x_sell_75pct`: sell 75% of current remaining inventory when executable bid-depth VWAP is at least 2x rolling average cost.
4. `tp_5x_sell_25pct`: sell 25% of current remaining inventory when executable bid-depth VWAP is at least 5x rolling average cost.

## Commitments

- Observe at least 50 city-date weather events before the first rule review.
- Do not modify exit rules before 50 events because one branch is temporarily ahead or behind.
- Do not delete losing events.
- Do not backfill historical winners.
- Do not add new take-profit multiples based on interim results.
- Do not stop recording because one rule temporarily underperforms.
- Treat 50 events as the first review checkpoint, not the final proof; continue to 100 events if the result is still noisy.

## Formal Start And Hashes

Config hash before formal start:

`38297196ec7f2077566eff9fae56fa59d207cd08bc1e28ce0676694348d5a62f`

Current simulator code hash before formal start:

`dc131c2f3986c115dd8219613213ccdf1ebc43f30f46da8dd349aa5c4ed22d5f`

Current reporting code hash before formal start:

`d391847a5386f658fc9575ba6571e77ce3fd1e3bfb89fbb0c20ba33c0ff6f1f2`

When `start-formal --confirm` runs, the system records the exact start time and hashes in `data/forward_v5/system_state.json` and appends an audit event.

## Fix Policy

If a program bug must be fixed after formal start:

- keep old files and logs,
- record the old and new hashes,
- mark whether the change is a pure technical repair or a strategy logic change,
- do not reinterpret earlier records without a separate audit note.
