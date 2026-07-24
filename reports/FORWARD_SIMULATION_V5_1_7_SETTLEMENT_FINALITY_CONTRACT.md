# Weather Forward Simulation v5.1.7-RC6

Generated at: 2026-07-23T03:30:19.119254+00:00

Status: PASS_FOR_FORMAL_START

This release adds full replay audit from raw orderbook evidence through normalized depth, fill traces, fees, inventory, and event PnL. It contains no wallet, signing, or real order functionality.

## Settlement Finality Contract
Final settlement rows must be supported by resolved final public evidence. Proposed, pending, disputed, or unknown winner states cannot be booked as final payouts.
