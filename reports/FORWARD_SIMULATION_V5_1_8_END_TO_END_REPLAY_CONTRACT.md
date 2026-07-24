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

## Replay Contract

`full-replay` treats these as authority only: signal registration evidence bytes, Gamma HTTP bytes, CLOB market HTTP bytes, CLOB orderbook HTTP bytes, settlement HTTP bytes, frozen config/code/schema/report hashes, and immutable run/lock timestamps.

Derived caches are never trusted as inputs: `signals`, `signal_hash`, `event_key`, bucket labels, fee fields, tick/min fields, entry_order_state, strategy_lots, allocations, settlements, event_results, strategy_totals, and ledger_totals are recomputed and compared.

Comparison precision: Decimal exact equality for shares, prices, fees, and PnL after the existing simulator quantization; stable canonical JSON SHA-256 for parsed JSON evidence; raw SHA-256 over exact stored HTTP bytes for raw evidence.

Key error codes include: SIGNAL_*_MISMATCH, MARKET_*_MISMATCH, FILL_FEE_*_MISMATCH, ENTRY_STATE_*_MISMATCH, LOT_*_MISMATCH, EXIT_ALLOCATION_*_MISMATCH, SETTLEMENT_ALLOCATION_*_MISMATCH, EVENT_PNL_MISMATCH, STRATEGY_PNL_MISMATCH, TOTAL_LEDGER_PNL_MISMATCH, and INCOMPLETE_TAKE_PROFIT_MISMATCH.
