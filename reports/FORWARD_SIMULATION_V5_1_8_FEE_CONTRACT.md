# Weather Forward Simulation v5.1.8-RC7

Generated at: 2026-07-23T05:38:54.309034+00:00

Status: PASS_FOR_FORMAL_START

This release extends replay from raw orderbook-to-fill evidence into end-to-end ledger reconstruction: signal evidence, market evidence, fees, constraints, entry state, lots, allocations, settlement, event, strategy, and total ledger PnL. It contains no wallet, signing, or real order functionality.

## Fee Contract
Official fill fee is recalculated as `shares * fee_rate * price * (1 - price)` when fee evidence is official. Disabled fees remain zero. Unknown, unsupported, or conflicting fees cannot be used for official formal fills.
