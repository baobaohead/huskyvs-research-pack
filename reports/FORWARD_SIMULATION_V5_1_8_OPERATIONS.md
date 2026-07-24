# Weather Forward Simulation v5.1.8-RC7

Generated at: 2026-07-23T05:38:54.309034+00:00

Status: PASS_FOR_FORMAL_START

This release extends replay from raw orderbook-to-fill evidence into end-to-end ledger reconstruction: signal evidence, market evidence, fees, constraints, entry state, lots, allocations, settlement, event, strategy, and total ledger PnL. It contains no wallet, signing, or real order functionality.

## Commands
- Register signal: `python3 -m src.forward_simulation_v5_1_8 --root ... --config config/forward_simulation_v5_1_8.yaml register-signal --mode formal --signals-file templates/entry_signal_v5_1_8.csv`
- Full audit: `python3 -m src.forward_simulation_v5_1_8 --root ... --config config/forward_simulation_v5_1_8.yaml audit-integrity --mode formal --level full-replay`
