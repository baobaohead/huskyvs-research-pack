# FORWARD_SIMULATION_V5_1_RC1_RELEASE_CHECKLIST

## Gate

- [x] v5.1 audit object hashed before review.
- [x] v5.1 audit object hashed after review.
- [x] v5.1 original files were not overwritten.
- [x] Blocking fixes were placed in v5.1.1 files.
- [x] SQLite main ledger added for RC1.
- [x] Schema hash included in formal freeze coverage.
- [x] No formal sample started.
- [x] Original `data/forward_v5_1/formal` remains empty.

## Adversarial Checks

- [x] Same-token entry signals share ask depth inside one strategy path.
- [x] Same-token exit triggers share bid depth inside one strategy branch.
- [x] Stable allocation is independent of input order.
- [x] Different strategy branches may replay independent order-book copies.
- [x] Entry crash failpoint rolls back.
- [x] Exit crash failpoint rolls back.
- [x] Settlement crash failpoint rolls back.
- [x] State write crash failpoint rolls back.
- [x] Hash drift in reporting code rejects formal writes.
- [x] Hash drift in schema rejects formal writes.
- [x] Missing or wrong config path does not fall back to v5 config.
- [x] Hash restore allows formal fixture flow to continue.
- [x] 50-event semantics pass.
- [x] Signal authenticity and freshness checks pass.
- [x] Fee and PnL conservation checks pass.
- [x] Settlement evidence checks pass.
- [x] Run-loop lock, stale lock, pause, resume, stop, and network recovery checks pass.
- [x] 17/17 negative integrity corruptions detected.

## Required Before Real Formal Start

- [ ] User explicitly confirms formal start.
- [ ] Use v5.1.1 RC1 files, not the v5.1 CSV/JSONL main ledger.
- [ ] Confirm public order-book adapter source for live monitoring.
- [ ] Confirm settlement evidence source workflow.
- [ ] Confirm fee assumptions are still acceptable as simulation assumptions.

