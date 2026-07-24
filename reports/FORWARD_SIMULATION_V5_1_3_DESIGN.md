# FORWARD_SIMULATION_V5_1_3_DESIGN

v5.1.3-RC2 is a standalone merge release. It combines the v5.1.1-RC1 SQLite ledger design with the v5.1.2 public read-only Polymarket adapter and removes dependency on prior version files.

Key design decisions:

- Formal `monitor-once` and `run-loop` use only `src/polymarket_public_adapter_v5_1_3.py` for market detail, token mapping, orderbook depth, fee parameters, and settlement status.
- Entry simulations consume asks by visible depth. Exit simulations consume bids by visible depth.
- All price, size, and fee math uses `Decimal`.
- `tick_size` and `min_order_size` from the orderbook are saved and enforced.
- Fee policy is CLOB-primary with Gamma crosscheck. Conflicts and unknowns do not enter official net simulation.
- Settlement is recorded only from official resolved evidence with an explicit winner or unambiguous token value map.
- Every monitor/live/demo run has a `run_id`; snapshots are unique per run by `UNIQUE(run_id, snapshot_id)`.
- Same content hash in the same run is idempotent and cannot create duplicate fills. The same content hash in a different run is retained as a separate observation.

Official references checked on 2026-07-22:

- https://docs.polymarket.com/trading/fees
- https://docs.polymarket.com/trading/clients/public
- https://docs.polymarket.com/api-reference/market-data/get-order-book
- https://docs.polymarket.com/api-reference/markets/get-market-by-slug
