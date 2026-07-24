# Weather Forward Simulation v5.1.7-RC6

Generated at: 2026-07-23T03:30:19.119254+00:00

Status: PASS_FOR_FORMAL_START

This release adds full replay audit from raw orderbook evidence through normalized depth, fill traces, fees, inventory, and event PnL. It contains no wallet, signing, or real order functionality.

## Fee Contract
Official fill fee is recalculated as `shares * fee_rate * price * (1 - price)` when fee evidence is official. Disabled fees remain zero. Unknown, unsupported, or conflicting fees cannot be used for official formal fills.
