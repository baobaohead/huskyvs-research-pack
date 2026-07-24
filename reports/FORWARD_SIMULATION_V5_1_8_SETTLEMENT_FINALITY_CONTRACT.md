# Weather Forward Simulation v5.1.8-RC7

Generated at: 2026-07-24T05:48:44.725540+00:00

Status: PASS_FOR_FORMAL_START

This release extends replay from raw orderbook-to-fill evidence into end-to-end ledger reconstruction: signal evidence, market evidence, fees, constraints, entry state, lots, allocations, settlement, event, strategy, and total ledger PnL. It contains no wallet, signing, or real order functionality.

## Validation separation
- saved public response replay: pass
- current live readonly selection: pass
- selected_market_count: 3
- selected_token_count: 3
- snapshot_count: 3
- error_count: 0
- raw_orderbook_evidence_count: 3
- raw_evidence_hash_result: pass
- snapshot_replay_result: pass
- same_run_evidence_chain: True
- blocked_reasons: []
- formal start: ALLOWED_BUT_NOT_STARTED

## Settlement Finality Contract
Final settlement rows must be supported by resolved final public evidence. Proposed, pending, disputed, or unknown winner states cannot be booked as final payouts.
