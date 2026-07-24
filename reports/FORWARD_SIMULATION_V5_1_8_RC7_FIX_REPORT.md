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

## Fixes
- Exact HTTP response bytes are stored in `http_evidence.raw_http_bytes`.
- Live-readonly persists Gamma/CLOB/orderbook evidence as JSON metadata plus sidecar `.bin` raw bytes; snapshots are accepted only after durable writes succeed.
- `real_signal_to_fill_validation` is derived from same-run saved evidence (`validation_source=live_readonly_saved_evidence`), not a second unsaved network fetch.
- Release gate requires `error_count==0`, raw evidence presence/hash integrity, snapshot replay from saved bytes, and same-run linkage; otherwise `BLOCKED_PENDING_LIVE_EVIDENCE`.
- Signal canonical hashes are rebuilt from registration evidence.
- Fees are recalculated from Gamma/CLOB evidence, not fill cache fields.
- Entry state is rebuilt from signal plus entry fills before any extra buy.
- Strategy lots, exit allocations, settlement allocations, event results, strategy totals, and ledger totals are replayed from fills and settlement evidence.
- Release status separates saved-response replay from current live-readonly selection.
