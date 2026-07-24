# FORWARD_SIMULATION_V5_1_RC1_AUDIT

## Conclusion

`PASS_FOR_FORMAL_START`

This conclusion applies to the RC1 fixed implementation in v5.1.1:

- `src/forward_simulation_v5_1_1.py`
- `src/forward_reporting_v5_1_1.py`
- `config/forward_simulation_v5_1_1.yaml`
- `schemas/forward_simulation_v5_1_1.sql`
- `tests/test_forward_simulation_v5_1_1.py`

The original v5.1 files were not overwritten. The adversarial review found two blocking issues in the v5.1 architecture: same-snapshot order-book depth could be reused across signals, and CSV/JSONL could not provide cross-file transaction guarantees. RC1 fixes those issues by moving the main ledger to SQLite and adding stable same-strategy depth allocation.

## Scope

No formal sample was started. No real signal was registered. No wallet, private key, signing, or real order submission exists in RC1. All RC1 tests run offline with deterministic fixtures and frozen time `2026-07-21T00:00:00+00:00`.

## Key Fixes

| Area | RC1 result |
| --- | --- |
| Shared order-book depth | Same token and same snapshot are allocated by `created_at_utc, signal_id`; one strategy branch cannot reuse the same depth across multiple signals. |
| Strategy independence | Different strategy branches remain mutually exclusive counterfactuals and may independently replay the same order book. |
| Transaction safety | Entry, lot creation, exit, trigger update, settlement, allocation, and state writes are SQLite transactions. Failpoint tests roll back cleanly. |
| Hash freeze | Formal writes check config, core code, reporting code, schema, and preregistration hashes. |
| Event count | `event_key = normalized_city | weather_date_local | weather_metric`; temperature bins are grouped under the event. |
| Signal authenticity | UTC timestamps required; system creates `registered_at_utc`; stale, future, before-start, duplicate-conflict, and metadata-mismatch signals are rejected. |
| Fees | Gross and net PnL are both stored; net PnL subtracts entry, exit, and settlement fee assumptions. |
| Settlement evidence | Source, source reference, source type, observed time, raw evidence, evidence hash, outcome, value, and notes are required. |
| Run-loop safety | Foreground only, default finite iterations, single-instance lock, stale lock recovery, pause/resume/stop, and per-token error isolation. |
| Integrity negative tests | 17/17 intentional corruptions were detected. |

## Test Evidence

- RC1 new tests: 28
- Full project tests: 69
- Sequential pytest run 1: `69 passed in 0.67s`
- Sequential pytest run 2: `69 passed in 0.65s`
- No skip, xfail, or warnings were reported by `pytest -q`.

## Data Evidence

- Negative integrity table: `data/forward_v5_1_rc1/integrity_negative_tests.csv`
- Adversarial result table: `data/forward_v5_1_rc1/adversarial_test_results.csv`
- Formal empty proof: `data/forward_v5_1_rc1/formal_empty_proof.json`
- Temporary formal rehearsal summary: `data/forward_v5_1_rc1/formal_rehearsal_summary.json`

## Remaining Limits

- RC1 still simulates execution from order-book snapshots; it is not a real exchange fill replay.
- Real exchange fees are not confirmed. RC1 preserves the conservative 10 bps entry and 10 bps exit simulation assumption and does not label it as real fees.
- Settlement still requires a verified evidence source or operator-supplied evidence.
- Market metadata consistency can be enforced when metadata is provided; without a trusted market metadata source, RC1 cannot independently prove that a token belongs to the stated city/date/metric.

