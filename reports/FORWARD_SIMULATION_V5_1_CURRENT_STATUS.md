# FORWARD_SIMULATION_V5_1_CURRENT_STATUS

Generated at: 2026-07-21T06:45:00.665444+00:00

## Formal Sample

Formal v5.1 has not been started in this delivery. Demo rows are isolated under `data/forward_v5_1/demo/`.

| Strategy | Signals | Positions | Traded Events | Settled Events | Entry Cost | Fees | Net PnL | Delta vs Hold |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| hold_to_settlement | 0 | 0 | 0 | 0 | $0.00 | $0.00 | n/a | $0.00 |
| tp_2x_sell_50pct | 0 | 0 | 0 | 0 | $0.00 | $0.00 | n/a | $0.00 |
| tp_2x_sell_75pct | 0 | 0 | 0 | 0 | $0.00 | $0.00 | n/a | $0.00 |
| tp_5x_sell_25pct | 0 | 0 | 0 | 0 | $0.00 | $0.00 | n/a | $0.00 |

## Demo Sample

| Strategy | Signals | Positions | Traded Events | Settled Events | Entry Cost | Fees | Net PnL | Delta vs Hold |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| hold_to_settlement | 1.0 | 1.0 | 1.0 | 1 | $100.00 | $0.10 | $-100.10 | $0.00 |
| tp_2x_sell_50pct | 1.0 | 1.0 | 1.0 | 1 | $100.00 | $0.22 | $24.77 | $124.88 |
| tp_2x_sell_75pct | 1.0 | 1.0 | 1.0 | 1 | $100.00 | $0.29 | $87.21 | $187.31 |
| tp_5x_sell_25pct | 1.0 | 1.0 | 1.0 | 1 | $100.00 | $0.10 | $-100.10 | $0.00 |

## Integrity

- Formal audit-integrity ok: True
- Demo audit-integrity ok: True
- No wallet, signing, or real order-submission code is present.
