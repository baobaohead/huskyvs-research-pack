# D1 registration gate v1

`register_signals` invokes `verify_d1_registration_bundle` before it opens a
ledger transaction when any submitted row has `source=d1_signal_bridge_v1`.
The gate accepts only the canonical CSV in a complete, non-symlinked bridge run
directory and delegates integrity plus semantic replay to
`verify_bridge_output`.

For D1 files, all rows must be from the same run and must equal the verified
CSV set.  The gate binds notes hashes and order-book identity to the replayed
core manifest, requires formal/execution flags, and makes registration
all-or-nothing.  Non-D1 files retain the existing compatibility path.

Successful D1 registrations persist a run-level record in
`d1_registration_evidence` and retain the existing per-signal evidence in
`signal_registration_evidence`.  This gate never starts formal mode, creates a
formal ledger, connects an account, signs, or places an order.
