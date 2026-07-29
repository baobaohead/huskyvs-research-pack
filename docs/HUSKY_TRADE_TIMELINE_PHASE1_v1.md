# Husky 公开天气交易逐笔时间线：一期

## 技术摘要

现有公开记录被评为 `PARTIAL_BUT_USABLE`。本期按确定性规则选择 8 个事件；其中 5 个通过严格完整性检查，8 个具有明确公开仓位 PnL 路径。公开时间只称 `public_record_timestamp`，不等同于原始挂单或订单提交时间。

`EXPLORATORY_CASE_SELECTION` · `NOT_A_RANDOM_SAMPLE` · `NOT_A_PROFITABILITY_VALIDATION`

## 样本内建仓主要集中在哪些阶段

- D-1_EARLY: $78.30 (12.2%)
- D-1_AFTERNOON: $29.49 (4.6%)
- D-1_EVENING: $84.39 (13.2%)
- D0_WARMING_EARLY: $127.91 (20.0%)
- D0_WARMING_CORE: $62.89 (9.8%)
- D0_LATE: $247.30 (38.6%)
- OUTSIDE_RESEARCH_WINDOW: $10.25 (1.6%)

样本中 D-1 首次建仓 4 个，D0 首次建仓 2 个。D-1 / D0 BUY 金额占比分别为 30.0% / 68.4%。50% 建仓相对时点中位数为 D0 14:31 CST。PRICE_UP_ADD / PRICE_DOWN_ADD / PRICE_FLAT_ADD 次数分别为 14 / 13 / 88。

## 温度篮子和退出

先单档后篮子的事件为 8 个；5 分钟窗口内形成多档的事件为 0 个。达到 50% 卖出阈值的事件为 4 个；没有记录 SELL 的事件为 0 个。首次卖出阶段分布：{"D0_LATE": 3, "D-1_AFTERNOON": 2, "D0_WARMING_CORE": 1, "D0_WARMING_EARLY": 1, "D-1_EARLY": 1}。

## 八个探索事件

- `2026-04-17__beijing__high`：BUY $90.52，41 笔，3 个温度档，SELL 5 笔，数据完整性 COMPLETE，PnL 状态 AUTHORITATIVE_POSITION_PNL_COMPLETE。
- `2026-03-21__beijing__high`：BUY $9.27，4 笔，2 个温度档，SELL 3 笔，数据完整性 COMPLETE，PnL 状态 AUTHORITATIVE_POSITION_PNL_COMPLETE。
- `2026-05-14__beijing__high`：BUY $37.48，4 笔，2 个温度档，SELL 2 笔，数据完整性 COMPLETE，PnL 状态 AUTHORITATIVE_POSITION_PNL_COMPLETE。
- `2026-02-11__ankara__high`：BUY $174.30，13 笔，3 个温度档，SELL 1 笔，数据完整性 COMPLETE，PnL 状态 AUTHORITATIVE_POSITION_PNL_COMPLETE。
- `2026-04-05__beijing__high`：BUY $17.96，2 笔，2 个温度档，SELL 4 笔，数据完整性 PARTIAL，PnL 状态 AUTHORITATIVE_POSITION_PNL_COMPLETE。
- `2026-04-22__seoul__high`：BUY $163.50，20 笔，6 个温度档，SELL 2 笔，数据完整性 PARTIAL，PnL 状态 AUTHORITATIVE_POSITION_PNL_COMPLETE。
- `2026-04-03__taipei__high`：BUY $81.43，35 笔，3 个温度档，SELL 4 笔，数据完整性 PARTIAL，PnL 状态 AUTHORITATIVE_POSITION_PNL_COMPLETE。
- `2026-03-05__london__high`：BUY $66.09，21 笔，4 个温度档，SELL 7 笔，数据完整性 COMPLETE，PnL 状态 AUTHORITATIVE_POSITION_PNL_COMPLETE。

## 三个可读案例

### A_分批建仓：2026-04-17__beijing__high

- 首次买入：2026-04-17T15:09:58+08:00
- 50% 建仓：2026-04-17T15:26:58+08:00
- 最后买入：2026-04-17T16:17:28+08:00
- 首次 / 最后卖出：2026-04-17T15:40:46+08:00 / 2026-04-17T15:55:46+08:00
- 主要投入阶段：D0_LATE
- 温度档形成顺序：22°C → 23°C → 24°C
- 补仓价格方向：{"NEW_BUCKET_ADD": 3, "PRICE_FLAT_ADD": 32, "PRICE_DOWN_ADD": 6}
- 数据完整性：COMPLETE
- 大白话结论：该事件公开 BUY 金额的 100.0% 发生在 D0_LATE，这是样本内的主要投入阶段。

### B_相邻档篮子：2026-03-21__beijing__high

- 首次买入：2026-03-21T15:57:53+08:00
- 50% 建仓：2026-03-21T16:15:19+08:00
- 最后买入：2026-03-21T16:21:51+08:00
- 首次 / 最后卖出：2026-03-21T16:04:31+08:00 / 2026-03-21T17:03:31+08:00
- 主要投入阶段：D0_LATE
- 温度档形成顺序：16°C → 15°C
- 补仓价格方向：{"NEW_BUCKET_ADD": 2, "PRICE_FLAT_ADD": 2}
- 数据完整性：COMPLETE
- 大白话结论：该事件公开 BUY 金额的 100.0% 发生在 D0_LATE，这是样本内的主要投入阶段。

### C_部分卖出或明显退出：2026-05-14__beijing__high

- 首次买入：2026-05-13T15:57:22+08:00
- 50% 建仓：2026-05-13T15:57:22+08:00
- 最后买入：2026-05-14T12:23:29+08:00
- 首次 / 最后卖出：2026-05-13T16:28:54+08:00 / 2026-05-14T12:23:48+08:00
- 主要投入阶段：D-1_AFTERNOON
- 温度档形成顺序：33°C or below → 35°C
- 补仓价格方向：{"NEW_BUCKET_ADD": 2, "PRICE_UP_ADD": 1, "PRICE_FLAT_ADD": 1}
- 数据完整性：COMPLETE
- 大白话结论：该事件公开 BUY 金额的 63.0% 发生在 D-1_AFTERNOON，这是样本内的主要投入阶段。

以上三个案例只用于展示还原方法，不得推广为 Husky 整体策略。

## 盈利与亏损案例的时间结构只作描述

盈利案例（2 个）D-1 / D0 BUY 金额占比分别为 53.7% / 46.3%，建仓持续时间中位数 17.16 小时。

亏损案例（6 个）D-1 / D0 BUY 金额占比分别为 27.8% / 70.5%，建仓持续时间中位数 28.98 小时。

这些差异来自非随机选择的 8 个探索案例，不能解释因果，也不能验证总体盈利率。

## 候选研究时点

- D1_1500_CANDIDATE: 截止前 13.8%，截止后 $551.98，MOST_SAMPLE_BUY_USD_REMAINS，INSUFFICIENT_FOR_FINAL_MODEL_SELECTION。
- D0_0800_CANDIDATE: 截止前 31.6%，截止后 $438.10，MATERIAL_SAMPLE_BUY_USD_REMAINS，INSUFFICIENT_FOR_FINAL_MODEL_SELECTION。
- D0_1000_CANDIDATE: 截止前 31.6%，截止后 $438.10，MATERIAL_SAMPLE_BUY_USD_REMAINS，INSUFFICIENT_FOR_FINAL_MODEL_SELECTION。
- D0_1100_CANDIDATE: 截止前 38.1%，截止后 $396.72，MATERIAL_SAMPLE_BUY_USD_REMAINS，INSUFFICIENT_FOR_FINAL_MODEL_SELECTION。
- D0_REALTIME_NOWCAST_CANDIDATE: REQUIRES_REALTIME_NOWCAST_DEFINITION，INSUFFICIENT_FOR_FINAL_MODEL_SELECTION。

当前 8 个确定性探索样本不足以选择最终模型时点；建议扩展到 20—30 个事件后复核。

## 数据、定义与方法

- 事件单位：`city + weather_date_local + weather_metric`。
- 所有显示时间统一为 Asia/Shanghai，并保留 UTC。
- 建仓 25%/50%/75% 按事件最终总 BUY 金额累计，不按笔数，也不在单笔内插值。
- 卖出阈值按事件总 BUY 份额累计。
- 交易金额优先用 activity 的 `usdcSize`；缺失时用 `price × size`。
- 去重键包含 timestamp、transactionHash、conditionId、asset、side、price、size；不会只按 transactionHash 去重。
- 多档 5 分钟窗口是本研究的确定性分类口径，不是 Husky 公布的规则。

## 数据质量审计结果

- 公开数据快照生成时间：2026-07-20T07:30:54.724715+00:00；采集范围 2025-11-01T00:00:00+00:00 至 2026-07-21T00:00:00+00:00。
- 原始 trades / activity 行数：18308 / 19498；复合键去重后 trades 18308 行。
- 天气交易 17964 行；transactionHash 覆盖 100.00%；weather_date 覆盖 100.00%。
- trades 与 activity 六字段关联覆盖 100.00%；size 精确一致 95.08%。
- 天气资产结算路径覆盖 56.70%；Husky proxyWallet 数量 1。

## 局限与可扩展结论

- 值得扩展：建仓完成时点、价格上涨/下跌后的补仓比例、相邻档加入顺序、首次卖出阶段。
- 目前不能成立：Husky 的主观预测档、原始挂单时间、北京市场对应 ZBAA、整体盈利率、最终可复制时点。
- 没有 SELL 不自动等于持有到结算；只有仓位生命周期证据完整时才标记观察到结算路径。
- FIFO 与移动平均成本仅归因公开 SELL；最终 position PnL 无法被任意按投入阶段平摊。

## 下一步

将相同规则扩展到 20—30 个确定性事件，并定向补齐缺失结算路径；在样本扩展前不选择最终跟随模型。
