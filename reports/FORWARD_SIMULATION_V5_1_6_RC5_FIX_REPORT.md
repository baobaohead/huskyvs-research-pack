# FORWARD_SIMULATION_V5_1_6_RC5_FIX_REPORT

## Fixes

- Market state gates entry, exit, and settlement before any orderbook consumption.
- Active trading markets allow entry and exit only; closed unresolved, resolution pending, disputed, unknown, and not-accepting-order states block entry/exit.
- Resolved markets write raw Gamma/CLOB evidence and run settlement only when a clear winner is available.
- One orderbook snapshot per token per monitor round is shared for all entry signals by `created_at_utc, signal_id`.
- Same-strategy exits share one bid-depth copy; different strategies replay independent counterfactual books.
- Strict UTC signal registration rejects future timestamps beyond 30 seconds, stale registrations beyond 300 seconds, non-UTC literals, and user-supplied registration timestamps.
- Formal frozen files include core, adapter, reporter, config, schema, preregistration, API contract, and fee contract.
- Run loop has foreground lock, heartbeat, pause/resume/stop, stale lock recovery, and infinite-loop confirmation.
- Fee disabled/conflict/exponent handling is explicit; unknown is not zero.
- Missing tick or min order size is rejected; Gamma/orderbook constraint conflicts are rejected.
