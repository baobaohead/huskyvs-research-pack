# FORWARD_SIMULATION_V5_1_3_RELEASE_AUDIT

Generated at: 2026-07-22T02:28:23.914380+00:00

## Conclusion

v5.1.3-RC2 merges the v5.1.1-RC1 ledger pattern with the v5.1.2 public adapter into a standalone module set.

## Blocking Fixes

- Public adapter is called by `monitor-once` and `run-loop` for market detail, CLOB fee parameters, order books, and settlement status.
- Buy fees increase cost; sell fees reduce proceeds; settlement fee status is explicit.
- Fee policy is CLOB-primary and Gamma-crosschecked; unknown/conflict does not become zero.
- Settlement requires resolved official evidence and never defaults a missing winner to zero.
- Token mapping crosschecks Gamma outcomes/token IDs, CLOB token mapping, orderbook asset/condition, and signal metadata.
- Every run writes a `run_id`; reports and manifests are run-aware.
- Tick size and min order size are parsed with Decimal and enforced before simulated fills.

## Integrity

- Formal empty proof: True
- Formal audit: True
- Demo audit: True

## Official API Notes

The implementation follows Polymarket public GET endpoints and fee/orderbook fields documented in the official Fees, Public Methods, Get Order Book, and Get Market by Slug pages.
