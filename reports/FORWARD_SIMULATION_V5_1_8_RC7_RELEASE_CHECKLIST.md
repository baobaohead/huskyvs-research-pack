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

- [x] Formal ledger empty
- [x] No wallet/signing/order code
- [x] ZIP self-contained target prepared
- [x] 30 direct end-to-end corruptions detected
- [x] incomplete_take_profit uses latest trigger state
- [x] Saved-response replay reported separately from live-readonly
- [x] Live-readonly selected markets and signal-to-fill pass
