# Husky 北京最高温市场全量公开交易深度分析 v1

## 技术摘要

截至 `2026-07-29T03:30:01.944885+00:00`，北京最高温公开历史仍为 50 个天气事件、537 笔公开成交（453 BUY / 84 SELL）。严格已关闭/结算口径覆盖 14 个事件，总 PnL $99.20。

本轮最重要修正是：`ACTIVE_OPEN_CONFIRMED` 只有 0 个；原先混在“当前开放”里的 36 个事件实际是已过 endDate、`redeemable=true` 的待赎回仓位。其隔离快照为 $-570.78，但由于权威资产重叠数为 0，验证结果是 `RESOLVED_UNREDEEMED_PNL_NOT_VALIDATED`，不得并入严格 PnL。

**公开成交笔数不是原始订单数；公开接口不展示未成交挂单或撤单。**

## 16 个审核问题的大白话答案

1. 当前可观察北京历史从 2026-03-21T15:57:53+08:00 开始；`ABSOLUTE_LIFETIME_FIRST_BEIJING_TRADE=NOT_PROVEN`。
2. 最新可观察成交为 2026-07-23T16:56:18+08:00，冻结点为 2026-07-29T03:30:01.944885+00:00。
3. 核心计数维持：50 个事件、537 笔公开成交。
4. D0 BUY 金额占比为 95.0%；“约 95% 在 D0 买入”的描述性结论维持。
5. 完整路径事件的首次/25%/50%/75%/最后建仓中位时点为 D0 05:20 CST / D0 11:18 CST / D0 12:49 CST / D0 13:41 CST / D0 14:34 CST。
6. 真正 ACTIVE_OPEN 为 0 个，活动仓位 MTM $0.00。
7. 已结算但未赎回为 36 个，隔离快照 $-570.78。
8. 已过 endDate 但状态不足为 0 个；其他仓位状态不明为 0 个。
9. 严格已结算总 PnL 为 $99.20，覆盖 14 个事件。
10. resolved-unredeemed PnL 不能验证：`RESOLVED_UNREDEEMED_PNL_NOT_VALIDATED`，且不进入严格 PnL。
11. 两种成本法都确认盈利的部分 SELL 有 8 个事件：2026-03-22, 2026-04-03, 2026-05-05, 2026-05-08, 2026-05-21, 2026-06-15, 2026-06-28, 2026-07-20。
12. 两种成本法都确认亏损的部分 SELL 有 4 个事件：2026-04-28, 2026-05-06, 2026-05-11, 2026-05-12。
13. 权威结算路径明确、归为持有到结算的事件有 14 个。
14. 最终路径未知分为：无 SELL 22 个、部分 SELL 后余量未知 14 个。
15. 盈亏完整路径事件的 50% 建仓中位时点分别为 D-1 23:24 CST / D0 14:39 CST；只表示观察性差异。
16. 最值得后续验证的是完整路径口径下的 D-1 15:00 与 D0 10:00、12:00、13:00、14:00、15:00、16:00；当前仍为 `INSUFFICIENT_FOR_FINAL_MODEL_SELECTION`。

## 可观察历史有高请求覆盖，但绝对生命周期起点未被证明

- `BEIJING_FIRST_OBSERVED_PUBLIC_TRADE=2026-03-21T15:57:53+08:00`
- `EARLIEST_OBSERVED_CURRENT_API_HISTORY_CONFIDENCE=HIGH`
- `ABSOLUTE_LIFETIME_FIRST_BEIJING_TRADE=NOT_PROVEN`
- `PUBLIC_REQUEST_COVERAGE=PASS`
- `OBSERVED_MONTH_COVERAGE=PASS`
- `ABSENCE_OF_UNOBSERVED_HISTORY_GAPS=NOT_PROVEN`

请求全部成功只证明本次公开接口请求完整返回，不能证明 API 从未遗漏、删除或截断过更早历史。

## 当前仓位被拆成互斥状态

| 事件状态 | 事件数 | 隔离 PnL | 是否进入严格 PnL |
|---|---:|---:|---|
| ACTIVE_OPEN_CONFIRMED | 0 | $0.00 | 否 |
| RESOLVED_REDEEMABLE_UNREDEEMED | 36 | $-570.78 | 否 |
| PAST_ENDDATE_STATUS_UNKNOWN | 0 | $0.00 | 否 |
| POSITION_STATUS_UNKNOWN | 0 | — | 否 |
| CLOSED_POSITION_CONFIRMED | 14 | $99.20 | 是 |

resolved 快照采用 `cashPnl + realizedPnl`，仅用于隔离观察。四个候选公式的权威重叠校验如下；没有重叠时“最稳定公式”必须保持未确定。

| 公式 | 可比资产数 | 精确匹配率 | 误差<0.01比例 | 最大绝对误差 |
|---|---:|---:|---:|---:|
| A_cashPnl | 0 | — | — | — |
| B_realizedPnl | 0 | — | — | — |
| C_cashPnl_plus_realizedPnl | 0 | — | — | — |
| D_currentValue_minus_initialValue_plus_realizedPnl | 0 | — | — | — |

`MOST_STABLE_FORMULA=UNDETERMINED_NO_AUTHORITATIVE_OVERLAP`；`SNAPSHOT_FORMULA_STATUS=UNVALIDATED_SNAPSHOT_ONLY`。

## SELL 标签只依据已记录 SELL 的实现盈亏

观察到部分退出 21 个事件；盈利部分 SELL 8 个，亏损部分 SELL 4 个，成本法明显不一致 4 个，成本路径不足 6 个。

FIFO 与平均成本法都为正，才标记 `PROFITABLE_PARTIAL_SELL_OBSERVED`；都为负，才标记 `LOSS_REALIZING_PARTIAL_SELL_OBSERVED`。最终事件盈利或亏损本身不会生成部分止盈/止损标签。

最终路径只有一个标签：

- `HOLD_TO_SETTLEMENT_OBSERVED`: 14
- `NO_RECORDED_SELL_FINAL_PATH_UNKNOWN`: 22
- `PARTIAL_EXIT_FINAL_PATH_UNKNOWN`: 14
- `FULL_RECORDED_EXIT`: 0
- `PATH_LABEL_MUTUAL_EXCLUSION=PASS`

## 盈利/亏损时间比较以完整建仓路径为主

主口径 `STRICT_PNL_ENTRY_COMPLETE_ONLY`：

| 结果 | 事件数 | 总BUY | 首次 | 25% | 50% | 75% | 最后 | 初始占比 | D-1占比 | D0占比 | D0 12后 | D0 14后 | D0 15后 |
|---|---:|---:|---|---|---|---|---|---:|---:|---:|---:|---:|---:|
| 盈利 | 4 | $136.41 | D-1 23:23 CST | D-1 23:23 CST | D-1 23:24 CST | D0 07:43 CST | D0 07:45 CST | 44.0% | 31.5% | 68.5% | 18.5% | 0.0% | 0.0% |
| 亏损 | 4 | $114.65 | D0 14:29 CST | D0 14:30 CST | D0 14:39 CST | D0 14:42 CST | D0 15:33 CST | 31.2% | 0.0% | 100.0% | 100.0% | 61.5% | 50.0% |

完整盈利/亏损的建仓时长中位数为 0.5434722222222222 / 0.6975 小时；相邻篮子占比为 25.0% / 50.0%；涨/跌/平价加仓计数分别为 4/3/23 与 0/7/39；首次/最后 SELL 中位时点为 D0 08:19 CST / D0 13:00 CST（盈利）和 D0 15:40 CST / D0 15:55 CST（亏损）。

次要敏感性口径 `STRICT_PNL_ALL`：

- 盈利事件 6 个，50% 建仓中位 D0 07:44 CST。
- 亏损事件 8 个，50% 建仓中位 D0 13:55 CST。

## 候选预测时点优先使用完整路径事件

| 时点 | 完整事件数 | 截止前资金 | 截止后资金 | 50%建仓已完成 | 此后仍买入 | 此后才首次买入 |
|---|---:|---:|---:|---:|---:|---:|
| D1_1500 | 36 | 0.9% | 99.1% | 1 | 0 | 35 |
| D0_1000 | 36 | 40.2% | 59.8% | 14 | 11 | 17 |
| D0_1200 | 36 | 40.9% | 59.1% | 16 | 11 | 15 |
| D0_1300 | 36 | 46.0% | 54.0% | 18 | 11 | 13 |
| D0_1400 | 36 | 62.8% | 37.2% | 22 | 9 | 10 |
| D0_1500 | 36 | 69.7% | 30.3% | 24 | 9 | 8 |
| D0_1600 | 36 | 85.2% | 14.8% | 27 | 11 | 4 |

`INSUFFICIENT_FOR_FINAL_MODEL_SELECTION`：这些时点只进入后续验证，本报告不冻结正式预测时点。

## 严格 PnL 保持独立

严格口径事件数 14，总 PnL $99.20，严格投入 $341.54，ROI 29.04%。resolved、active-open 和状态不明快照均没有并入该总数。

| 天气日 | 严格PnL | BUY金额 | 建仓路径 | 最终路径 |
|---|---:|---:|---|---|
| 2026-04-05 | 86.71 | 17.96 | ENTRY_TIMELINE_PARTIAL_UNMATCHED_SELL | HOLD_TO_SETTLEMENT_OBSERVED |
| 2026-03-22 | 58.11 | 14.00 | ENTRY_TIMELINE_COMPLETE | HOLD_TO_SETTLEMENT_OBSERVED |
| 2026-04-20 | 32.98 | 35.29 | ENTRY_TIMELINE_PARTIAL_UNMATCHED_SELL | HOLD_TO_SETTLEMENT_OBSERVED |
| 2026-05-14 | 20.82 | 37.48 | ENTRY_TIMELINE_COMPLETE | HOLD_TO_SETTLEMENT_OBSERVED |
| 2026-05-21 | 19.26 | 56.01 | ENTRY_TIMELINE_COMPLETE | HOLD_TO_SETTLEMENT_OBSERVED |
| 2026-05-06 | 17.36 | 28.92 | ENTRY_TIMELINE_COMPLETE | HOLD_TO_SETTLEMENT_OBSERVED |
| 2026-05-12 | -0.13 | 1.87 | ENTRY_TIMELINE_COMPLETE | HOLD_TO_SETTLEMENT_OBSERVED |
| 2026-04-19 | -0.73 | 0.75 | ENTRY_TIMELINE_PARTIAL_RECONCILIATION | HOLD_TO_SETTLEMENT_OBSERVED |
| 2026-04-13 | -4.03 | 4.23 | ENTRY_TIMELINE_PARTIAL_RECONCILIATION | HOLD_TO_SETTLEMENT_OBSERVED |
| 2026-03-21 | -5.27 | 9.27 | ENTRY_TIMELINE_COMPLETE | HOLD_TO_SETTLEMENT_OBSERVED |
| 2026-04-04 | -10.94 | 11.44 | ENTRY_TIMELINE_PARTIAL_RECONCILIATION | HOLD_TO_SETTLEMENT_OBSERVED |
| 2026-04-18 | -12.99 | 12.99 | ENTRY_TIMELINE_COMPLETE | HOLD_TO_SETTLEMENT_OBSERVED |
| 2026-04-06 | -20.01 | 20.82 | ENTRY_TIMELINE_PARTIAL_RECONCILIATION | HOLD_TO_SETTLEMENT_OBSERVED |
| 2026-04-17 | -81.94 | 90.52 | ENTRY_TIMELINE_COMPLETE | HOLD_TO_SETTLEMENT_OBSERVED |

## 口径、限制与下一步

- 事件单位是北京天气日，温度档不是独立事件样本。
- 当前仓位状态按冻结点与 `endDate`、`redeemable` 和权威关闭证据分类；只有 `ACTIVE_OPEN_CONFIRMED` 算开放。
- recorded SELL PnL 只为行为标签服务，不重复加入事件最终 PnL。
- SELL 两成本法方向不同，或绝对差异超过 `max($0.01, 两法较大绝对值的10%)`，即标记方法不一致。
- 完整路径口径是盈利/亏损建仓时间比较与候选预测时点的主口径；全部严格 PnL 仅作为敏感性结果。
- 公开请求成功不证明绝对历史完整；公开成交也不证明原始挂单、撤单、主观预测或因果策略。
- 北京结算站仍为 `BEIJING_STATION_UNCONFIRMED`，本轮没有改动 ZBAA。

下一步只应验证候选快照时点与完整路径事件上的稳健性；在 resolved PnL 获得权威重叠前，不得将其用于总收益、胜率或模型标签。
