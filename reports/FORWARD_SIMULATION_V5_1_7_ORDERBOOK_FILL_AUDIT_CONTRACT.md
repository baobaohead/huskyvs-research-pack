# Weather Forward Simulation v5.1.7-RC6

Generated at: 2026-07-23T03:30:19.119254+00:00

Status: PASS_FOR_FORMAL_START

This release adds full replay audit from raw orderbook evidence through normalized depth, fill traces, fees, inventory, and event PnL. It contains no wallet, signing, or real order functionality.

## Contract
Audit level `full-replay` must start from `raw_response`, recompute `raw_response_sha256`, rebuild the normalized book with `orderbook_normalize_v5_1_7_rc6`, then replay fills with `depth_replay_v5_1_7_rc6`.
