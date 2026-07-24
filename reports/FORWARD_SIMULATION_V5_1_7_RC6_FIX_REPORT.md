# Weather Forward Simulation v5.1.7-RC6

Generated at: 2026-07-23T03:30:19.119254+00:00

Status: PASS_FOR_FORMAL_START

This release adds full replay audit from raw orderbook evidence through normalized depth, fill traces, fees, inventory, and event PnL. It contains no wallet, signing, or real order functionality.

## Fixes
- Raw orderbook response hashes are recomputed.
- Normalized orderbook hashes are recomputed.
- Entry and exit fills are replayed level by level.
- Fees and net amounts are recalculated from stored fee fields.
