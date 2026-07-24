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

## Fixes
- Exact HTTP response bytes are stored in `http_evidence.raw_http_bytes`.
- Signal canonical hashes are rebuilt from registration evidence.
- Fees are recalculated from Gamma/CLOB evidence, not fill cache fields.
- Entry state is rebuilt from signal plus entry fills before any extra buy.
- Strategy lots, exit allocations, settlement allocations, event results, strategy totals, and ledger totals are replayed from fills and settlement evidence.
- Release status separates saved-response replay from current live-readonly selection.
