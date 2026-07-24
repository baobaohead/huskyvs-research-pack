# FORWARD_SIMULATION_V5_1_RC1_STATUS

## RC1 Status

- Conclusion: `PASS_FOR_FORMAL_START`
- Code modified: yes, via new v5.1.1 files only.
- v5.1 original files overwritten: no.
- Formal sample started: no.
- Real signals registered: no.
- Wallet/private key/real order submission: none.

## Test Status

- RC1 new tests: 28
- Full project tests: 69
- Sequential pytest run 1: `69 passed in 0.67s`
- Sequential pytest run 2: `69 passed in 0.65s`
- Offline fixture time: `2026-07-21T00:00:00+00:00`
- Random seed: not used.

## Integrity Status

- 17/17 negative integrity cases detected.
- Original v5.1 formal ledger proof: `ok: true`
- Temporary formal rehearsal: `audit_integrity_ok: true`

## Formal Empty Proof

The original v5.1 formal directory remains empty:

- `formal_started_at_utc = null`
- `signals.csv = 0 rows`
- `entry_fills.csv = 0 rows`
- `exit_fills.csv = 0 rows`
- `settlements.csv = 0 rows`
- `event_results.csv = 0 rows`

Detailed proof is stored in `data/forward_v5_1_rc1/formal_empty_proof.json`.

## Demo Rehearsal

The rehearsal used a temporary formal ledger and then deleted it. It covered:

- formal initialization and start in temp root
- fresh signal registration
- partial entry and later completion
- identical entry positions across four strategies
- one-time take-profit with insufficient depth and later completion
- pause and resume
- fixture network failure and recovery
- evidence-backed settlement
- event result generation
- audit-integrity and status

