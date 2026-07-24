# Weather Forward Simulation v5.1.7-RC6

Generated at: 2026-07-23T03:30:19.119254+00:00

Status: PASS_FOR_FORMAL_START

This release adds full replay audit from raw orderbook evidence through normalized depth, fill traces, fees, inventory, and event PnL. It contains no wallet, signing, or real order functionality.

## Commands
- Register signal: `python3 -m src.forward_simulation_v5_1_7 --root ... --config config/forward_simulation_v5_1_7.yaml register-signal --mode formal --signals-file templates/entry_signal_v5_1_7.csv`
- Full audit: `python3 -m src.forward_simulation_v5_1_7 --root ... --config config/forward_simulation_v5_1_7.yaml audit-integrity --mode formal --level full-replay`
