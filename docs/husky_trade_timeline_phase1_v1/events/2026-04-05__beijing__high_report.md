# 2026-04-05__beijing__high 逐笔交易还原

该事件公开 BUY 金额的 65.9% 发生在 D0_WARMING_CORE，这是样本内的主要投入阶段。

## 结论先行

- 首次买入：2026-04-04T23:43:29+08:00
- 完成 25% / 50% / 75% 建仓：2026-04-04T23:43:29+08:00 / 2026-04-05T13:36:03+08:00 / 2026-04-05T13:36:03+08:00
- 最后买入：2026-04-05T13:36:03+08:00
- 温度档形成顺序：20°C → 21°C
- 篮子类型：SINGLE_THEN_BASKET；dominant_bought_bucket=21°C
- 补仓方向：PRICE_UP_ADD=0，PRICE_DOWN_ADD=0，PRICE_FLAT_ADD=0。
- 卖出：首次 2026-04-05T13:36:09+08:00；最后 2026-04-05T14:12:45+08:00；共 4 笔。
- 记录内未卖份额：-5.265783；路径状态：RECORDED_SELL_PATH。
- 数据完整性：PARTIAL；盈亏路径：AUTHORITATIVE_POSITION_PNL_COMPLETE；总事件 PnL=86.71。

## 建仓和温度篮子

该事件共记录 2 笔 BUY，金额 $17.96，买入持续 13.88 小时。第一个温度档为 20°C；金额最高的档为 21°C。相邻整数温度档对为 [["20°C", "21°C"]]。

## 卖出和盈亏

首次 2026-04-05T13:36:09+08:00；最后 2026-04-05T14:12:45+08:00；共 4 笔。 以事件总 BUY 份额为分母，50% 卖出阈值状态为 REACHED（时间：2026-04-05T14:12:45+08:00）。

记录内 SELL 的 FIFO 实现盈亏为 $81.24；移动平均成本实现盈亏为 $81.24。归因稳定性：STABLE_WITHIN_THRESHOLD。两者都是本研究口径，不代表 Husky 的会计方法。

按原始买入阶段归因的 FIFO 结果：{"D-1_EVENING": 16.850252, "D0_WARMING_CORE": 64.3932033}；移动平均成本结果：{"D-1_EVENING": 16.850252, "D0_WARMING_CORE": 64.3932033}。状态：RECORDED_SELLS_ONLY_NO_SETTLEMENT_PNL_ALLOCATION。

## 证据等级

### OBSERVED

- 逐笔 BUY/SELL、价格、份额与 public_record_timestamp 来自公开记录。
- 温度档加入顺序、累计买入金额和记录内剩余份额由公开成交顺序直接计算。

### INFERRED

- 时间阶段是本研究按北京时间划分，不是 Husky 公布的规则。
- 篮子形成类别由各温度档首次公开成交时间的间隔推断。

### UNKNOWN

- 原始挂单时间、订单提交时间和撮合引擎精确时间不可由这些公开记录确定。
- 没有独立公开证据证明北京市场对应 ZBAA 站。

本案例属于 `EXPLORATORY_CASE_SELECTION`、`NOT_A_RANDOM_SAMPLE`、`NOT_A_PROFITABILITY_VALIDATION`，不得外推为 Husky 整体策略。
