# Weather Forward Simulation v5.1.8-RC7

Generated at: 2026-07-24T03:19:58.906076+00:00

Status: PASS_FOR_FORMAL_START

This release extends replay from raw orderbook-to-fill evidence into end-to-end ledger reconstruction: signal evidence, market evidence, fees, constraints, entry state, lots, allocations, settlement, event, strategy, and total ledger PnL. It contains no wallet, signing, or real order functionality.

## Validation separation
- saved public response replay: pass
- current live readonly selection: pass
- selected_market_count: 3
- selected_token_count: 3
- formal start: ALLOWED_BUT_NOT_STARTED

## Commands
- Register signal: `python3 -m src.forward_simulation_v5_1_8 --root . --config config/forward_simulation_v5_1_8.yaml register-signal --mode demo --signals-file templates/entry_signal_v5_1_8.csv`
- Full audit: `python3 -m src.forward_simulation_v5_1_8 --root . --config config/forward_simulation_v5_1_8.yaml audit-integrity --mode demo --level full-replay`
- Live readonly: `python3 -m src.forward_simulation_v5_1_8 --root . --config config/forward_simulation_v5_1_8.yaml live-integration --iterations 1`
