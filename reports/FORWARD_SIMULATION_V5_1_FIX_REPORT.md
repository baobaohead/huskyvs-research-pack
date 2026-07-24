# FORWARD_SIMULATION_V5_1_FIX_REPORT

## Scope

v5.1 implements the blocking fixes requested for the weather-market forward simulator. It does not overwrite v5 files or v1-v4 research outputs.

## Blocking Fixes

| Blocking issue | v5.1 status |
| --- | --- |
| Repeated take-profit sells after the same threshold | Fixed. Each `signal_id + strategy_id + trigger_stage_id` has one fixed trigger target. |
| 50 percent and 75 percent strategies repeatedly shrinking inventory on later high snapshots | Fixed and tested with three repeated high snapshots. |
| Partial take-profit fills being recalculated instead of continued | Fixed. Remaining trigger shares stay fixed until completed. |
| Event key using slug, condition, token, or temperature bucket | Fixed. Event key is normalized city + local date + metric. |
| 50-event threshold counting raw positions | Fixed. Reporting counts settled traded city-date-metric events. |
| Partial entry unable to keep filling | Fixed. Entry state tracks intended, filled, remaining, status, deadline, and last attempt reason. |
| Same token with multiple signals mixing accounting | Fixed. v5.1 uses signal-independent FIFO lots and signal-level strategy branches. |
| Exit traceability gaps | Fixed. Exit fills allocate to trigger, lot, entry fill, signal, token, event, and strategy. |
| Formal config/code drift still allowing writes | Fixed. Formal writes require hash context and recheck frozen hashes. |
| Stale or future formal signals | Fixed. Formal registration rejects before-start, stale, and future timestamps. |
| Fees absent from net PnL | Fixed. Entry, exit, and settlement fee fields feed net PnL. |
| Background daemon risk | Fixed. Run-loop is foreground-only with explicit iteration count or explicit infinite foreground mode. |
| Duplicate snapshot accounting | Fixed. Same signal/trigger/snapshot cannot be booked twice. |
| Settlement conflicts and exits after settlement | Fixed. Conflicts are rejected and settled strategy branches do not exit. |
| Missing audit-integrity command | Fixed. `audit-integrity` checks duplicates, inventory, trigger overfill, strategy consistency, demo pollution, hash drift, after-settlement exits, and formal timeouts. |

## Test Evidence

- v5.1 tests: 20 passed.
- Full project tests: 41 passed.
- No skipped or xfailed tests were reported.

## Demo Boundary

The offline demo writes only to `data/forward_v5_1/demo/`. Formal ledgers stay initialized and empty until the user explicitly runs `start-formal --confirm` and registers real forward signals.

## Remaining Interface Gaps

- Public order-book snapshots are not a historical order-book replay.
- Real fee schedule needs confirmation if exchange-level fees differ from the conservative 10 bps entry and 10 bps exit proxy.
- Settlement still requires operator-provided evidence or a confirmed public settlement source.
- Order-book depth proves simulated availability at snapshot time, not guaranteed execution in a real account.

