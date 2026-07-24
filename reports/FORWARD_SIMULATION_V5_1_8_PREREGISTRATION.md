# Weather Forward Simulation v5.1.8-RC7

Generated at: 2026-07-23T05:38:54.309034+00:00

Status: PASS_FOR_FORMAL_START

This release extends replay from raw orderbook-to-fill evidence into end-to-end ledger reconstruction: signal evidence, market evidence, fees, constraints, entry state, lots, allocations, settlement, event, strategy, and total ledger PnL. It contains no wallet, signing, or real order functionality.

The four exit rules remain frozen from v5: hold to settlement, 2x sell 50%, 2x sell 75%, and 5x sell 25%. No formal sample has been started in this release task.
