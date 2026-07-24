# FORWARD_SIMULATION_V5_CURRENT_STATUS

Generated at: 2026-07-21T05:42:17.218092+00:00

## Formal Sample

正式样本目前只包含系统启用后记录的数据；demo 数据在独立目录，不进入正式统计。

| Strategy | Events | Entry Spent | Unfilled | Settled | Net PnL | Delta vs Hold |
| --- | --- | --- | --- | --- | --- | --- |
| hold_to_settlement | 0 | $0.00 | n/a | 0 | n/a | $0.00 |
| tp_2x_sell_50pct | 0 | $0.00 | n/a | 0 | n/a | $0.00 |
| tp_2x_sell_75pct | 0 | $0.00 | n/a | 0 | n/a | $0.00 |
| tp_5x_sell_25pct | 0 | $0.00 | n/a | 0 | n/a | $0.00 |

## Demo Walkthrough

| Strategy | Events | Entry Spent | Unfilled | Settled | Net PnL | TP Events | Partial TP |
| --- | --- | --- | --- | --- | --- | --- | --- |
| hold_to_settlement | 1 | $100.00 | 0.0% | 1 | $-100.00 | 0 | 0 |
| tp_2x_sell_50pct | 1 | $100.00 | 0.0% | 1 | $139.86 | 1 | 0 |
| tp_2x_sell_75pct | 1 | $100.00 | 0.0% | 1 | $256.80 | 1 | 0 |
| tp_5x_sell_25pct | 1 | $100.00 | 0.0% | 1 | $-100.00 | 0 | 0 |

## Health Checks

- Formal rows: 0 event-strategy rows.
- Demo rows: 4 event-strategy rows.
- No real trading or wallet connection is implemented in this system.
